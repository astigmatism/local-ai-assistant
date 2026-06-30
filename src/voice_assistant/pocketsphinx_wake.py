from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, TextIO

DEFAULT_HMM_DIR = "/usr/share/pocketsphinx/model/en-us/en-us"
DEFAULT_DICT_PATH = "/usr/share/pocketsphinx/model/en-us/cmudict-en-us.dict"
DEFAULT_CUSTOM_DICT_PATH = "/tmp/voice-assistant-pocketsphinx/wake-custom.dict"
DEFAULT_DEVICE = "plughw:0,0"
DEFAULT_SAMPLE_RATE = 16000
DEFAULT_CHANNELS = 1
DEFAULT_PHRASE = "Rosalina"
DEFAULT_WINDOW_SECONDS = 4.0
DEFAULT_HOP_SECONDS = 1.0
DEFAULT_COOLDOWN_SECONDS = 1.5
DEFAULT_DECODER_GRACE_SECONDS = 5.0
ENGINE_NAME = "pocketsphinx_continuous_arecord_overlap"
CAPTURE_BACKEND = "arecord_stream_overlap"
PCM_SAMPLE_WIDTH_BYTES = 2

# Debian's pocketsphinx-en-us dictionary does not consistently include proper names. Keep the
# default wake phrase deterministic by adding local runtime entries when needed instead of asking an
# operator to edit the container's system dictionary. The primary pronunciation is the common English
# "roh-zuh-LEE-nuh" reading. The alternate covers speakers who pronounce the spelling's "s" less
# like /z/.
CUSTOM_PRONUNCIATIONS: dict[str, tuple[str, ...]] = {
    "rosalina": (
        "R OW Z AH L IY N AH",
        "R OW S AH L IY N AH",
    )
}

_WORD_RE = re.compile(r"[a-z0-9']+")
_DICT_VARIANT_RE = re.compile(r"\(\d+\)$")
_capture_child: subprocess.Popen[bytes] | None = None
_decoder_child: subprocess.Popen[bytes] | None = None
_stop_requested = False


@dataclass(frozen=True)
class DictionaryResolution:
    path: str
    base_path: str
    custom_path: str | None = None
    added_pronunciations: dict[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class DecodeResult:
    detected: bool
    detected_line: str | None
    last_decoder_line: str | None
    decoder_exit_code: int | None
    bytes_decoded: int


@dataclass
class DetectionStats:
    chunks_processed: int = 0
    detections: int = 0
    last_detection_phrase: str | None = None
    last_detection_raw: str | None = None
    last_raw_recognizer_line: str | None = None
    last_decoder_exit_code: int | None = None


def _normalise(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


def _contains_phrase(line: str, phrase: str) -> bool:
    phrase_words = _normalise(phrase)
    line_words = _normalise(line)
    if not phrase_words or not line_words:
        return False
    width = len(phrase_words)
    return any(line_words[index : index + width] == phrase_words for index in range(0, len(line_words) - width + 1))


def _decoder_keyphrase(phrase: str) -> str:
    words = _normalise(phrase)
    return " ".join(words) if words else phrase.strip().lower()


def threshold_from_sensitivity(sensitivity: float) -> str:
    """Map the app's 0..1 sensitivity to PocketSphinx's keyword threshold.

    PocketSphinx keyword thresholds are tiny probability-ratio values. In practice, lower values are
    more permissive. This mapping keeps the documented app knob simple while still allowing operators
    to override the exact value with --threshold or VOICE_ASSISTANT_POCKETSPHINX_THRESHOLD.
    """

    clipped = min(1.0, max(0.0, sensitivity))
    exponent = -5 - round(clipped * 30)
    return f"1e{exponent}"


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_first_float(names: tuple[str, ...], default: float) -> float:
    for name in names:
        value = os.getenv(name)
        if value is None:
            continue
        try:
            return float(value)
        except ValueError:
            continue
    return default


def _env_text(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip()


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return parsed


def _non_negative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be greater than or equal to 0")
    return parsed


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local PocketSphinx wake-word stdout adapter")
    parser.add_argument("--phrase", default=_env_text("VOICE_ASSISTANT_WAKE_PHRASE", DEFAULT_PHRASE))
    parser.add_argument("--device", default=_env_text("VOICE_ASSISTANT_CAPTURE_DEVICE", DEFAULT_DEVICE))
    parser.add_argument("--sample-rate", type=_positive_int, default=_env_int("VOICE_ASSISTANT_SAMPLE_RATE_HZ", DEFAULT_SAMPLE_RATE))
    parser.add_argument("--channels", type=_positive_int, default=_env_int("VOICE_ASSISTANT_CHANNELS", DEFAULT_CHANNELS))
    parser.add_argument(
        "--window-seconds",
        "--chunk-seconds",
        dest="window_seconds",
        type=_positive_float,
        default=_env_first_float(("VOICE_ASSISTANT_WAKE_WINDOW_SECONDS", "VOICE_ASSISTANT_WAKE_CHUNK_SECONDS"), DEFAULT_WINDOW_SECONDS),
        help="decode window length in seconds; --chunk-seconds is accepted as a backward-compatible alias",
    )
    parser.add_argument(
        "--hop-seconds",
        type=_positive_float,
        default=_env_float("VOICE_ASSISTANT_WAKE_HOP_SECONDS", DEFAULT_HOP_SECONDS),
        help="seconds between overlapping decode windows; default gives 75 percent overlap for a 4-second window",
    )
    parser.add_argument(
        "--cooldown-seconds",
        type=_non_negative_float,
        default=_env_float("VOICE_ASSISTANT_WAKE_COOLDOWN_SECONDS", DEFAULT_COOLDOWN_SECONDS),
        help="debounce delay after an emitted wake detection",
    )
    parser.add_argument(
        "--decoder-grace-seconds",
        type=_positive_float,
        default=_env_float("VOICE_ASSISTANT_WAKE_DECODER_GRACE_SECONDS", DEFAULT_DECODER_GRACE_SECONDS),
        help="extra time allowed for PocketSphinx to finish each overlapping decode window",
    )
    parser.add_argument("--hmm", default=_env_text("VOICE_ASSISTANT_WAKE_MODEL_PATH", DEFAULT_HMM_DIR))
    parser.add_argument("--dict", dest="dict_path", default=_env_text("VOICE_ASSISTANT_POCKETSPHINX_DICT", DEFAULT_DICT_PATH))
    parser.add_argument(
        "--custom-dict-path",
        default=_env_text("VOICE_ASSISTANT_POCKETSPHINX_CUSTOM_DICT", DEFAULT_CUSTOM_DICT_PATH),
        help="runtime path for a merged PocketSphinx dictionary when the wake phrase is absent from the base dictionary",
    )
    parser.add_argument(
        "--threshold",
        default=_env_text(
            "VOICE_ASSISTANT_POCKETSPHINX_THRESHOLD",
            threshold_from_sensitivity(_env_float("VOICE_ASSISTANT_WAKE_SENSITIVITY", 0.5)),
        ),
    )
    parser.add_argument("--self-test", action="store_true", help="Validate local wake prerequisites and exit")
    parser.add_argument(
        "--max-chunks",
        type=_positive_int,
        default=None,
        help="debug/test only: exit after processing this many overlapping decode windows",
    )
    parser.add_argument(
        "--diagnostic-summary",
        action="store_true",
        help="emit a final JSON diagnostic_summary with window, threshold, dictionary, and detection counts",
    )
    return parser


def _bytes_per_second(args: argparse.Namespace) -> int:
    return int(args.sample_rate) * int(args.channels) * PCM_SAMPLE_WIDTH_BYTES


def _aligned_byte_count(seconds: float, args: argparse.Namespace) -> int:
    raw = max(PCM_SAMPLE_WIDTH_BYTES, int(round(seconds * _bytes_per_second(args))))
    remainder = raw % PCM_SAMPLE_WIDTH_BYTES
    return raw if remainder == 0 else raw + (PCM_SAMPLE_WIDTH_BYTES - remainder)


def window_bytes(args: argparse.Namespace) -> int:
    return _aligned_byte_count(float(args.window_seconds), args)


def hop_bytes(args: argparse.Namespace) -> int:
    return _aligned_byte_count(float(args.hop_seconds), args)


def overlap_seconds(args: argparse.Namespace) -> float:
    return max(0.0, float(args.window_seconds) - float(args.hop_seconds))


def validate_overlap_settings(args: argparse.Namespace) -> list[str]:
    problems: list[str] = []
    if float(args.hop_seconds) > float(args.window_seconds):
        problems.append("hop_seconds must be less than or equal to window_seconds")
    if float(args.hop_seconds) > float(args.window_seconds) / 2.0:
        problems.append("hop_seconds must be no more than half of window_seconds so decode windows overlap by at least 50 percent")
    if int(args.channels) != 1:
        problems.append("PocketSphinx wake is packaged and tested for mono capture; set channels to 1")
    return problems


def build_arecord_command(args: argparse.Namespace) -> list[str]:
    """Build the continuous ALSA capture command used for overlapping wake audio.

    We intentionally capture with ``arecord`` instead of PocketSphinx's ``-inmic yes`` live
    microphone backend. On the target thin client ``arecord -D plughw:0,0`` can open the EMEET
    microphone reliably while ``pocketsphinx_continuous -inmic yes -adcdev plughw:0,0`` exits with
    "Connection refused". The wrapper keeps one local raw PCM capture open, snapshots the recent
    rolling audio buffer every hop interval, and sends each finite overlapping snapshot to
    ``pocketsphinx_continuous -infile /dev/stdin`` so the decoder still receives EOF per window.
    """

    return [
        "arecord",
        "-q",
        "-D",
        args.device,
        "-f",
        "S16_LE",
        "-r",
        str(args.sample_rate),
        "-c",
        str(args.channels),
        "-t",
        "raw",
    ]


def _dictionary_words(path: Path) -> set[str]:
    words: set[str] = set()
    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#") or line.startswith(";"):
                continue
            entry = line.split(maxsplit=1)[0].strip().lower()
            if not entry:
                continue
            words.add(_DICT_VARIANT_RE.sub("", entry))
    return words


def resolve_dictionary(args: argparse.Namespace) -> DictionaryResolution:
    base_path = Path(args.dict_path)
    phrase_words = list(dict.fromkeys(_normalise(args.phrase)))
    present_words = _dictionary_words(base_path)
    missing_words = [word for word in phrase_words if word not in present_words]
    if not missing_words:
        return DictionaryResolution(path=str(base_path), base_path=str(base_path))

    unknown_words = [word for word in missing_words if word not in CUSTOM_PRONUNCIATIONS]
    if unknown_words:
        raise ValueError(
            "wake phrase word(s) are absent from the PocketSphinx dictionary and have no built-in custom pronunciation: "
            + ", ".join(unknown_words)
        )

    custom_path = Path(args.custom_dict_path)
    custom_path.parent.mkdir(parents=True, exist_ok=True)
    added: dict[str, tuple[str, ...]] = {word: CUSTOM_PRONUNCIATIONS[word] for word in missing_words}
    base_text = base_path.read_text(encoding="utf-8", errors="ignore")
    entry_lines: list[str] = []
    for word in missing_words:
        for index, pronunciation in enumerate(CUSTOM_PRONUNCIATIONS[word]):
            label = word if index == 0 else f"{word}({index + 1})"
            entry_lines.append(f"{label} {pronunciation}")
    merged = base_text.rstrip() + "\n" + "\n".join(entry_lines) + "\n"
    tmp_path = custom_path.with_suffix(custom_path.suffix + ".tmp")
    tmp_path.write_text(merged, encoding="utf-8")
    tmp_path.replace(custom_path)
    return DictionaryResolution(path=str(custom_path), base_path=str(base_path), custom_path=str(custom_path), added_pronunciations=added)


def _dictionary_resolution(args: argparse.Namespace) -> DictionaryResolution:
    resolution = getattr(args, "dictionary_resolution", None)
    if isinstance(resolution, DictionaryResolution):
        return resolution
    resolution = resolve_dictionary(args)
    setattr(args, "dictionary_resolution", resolution)
    setattr(args, "effective_dict_path", resolution.path)
    return resolution


def build_pocketsphinx_command(args: argparse.Namespace) -> list[str]:
    resolution = _dictionary_resolution(args)
    command = [
        "pocketsphinx_continuous",
        "-infile",
        "/dev/stdin",
        "-samprate",
        str(args.sample_rate),
        "-hmm",
        args.hmm,
        "-dict",
        resolution.path,
        "-keyphrase",
        _decoder_keyphrase(args.phrase),
        "-kws_threshold",
        str(args.threshold),
        "-logfn",
        "/dev/null",
    ]
    if shutil.which("stdbuf") is not None:
        return ["stdbuf", "-oL", "-eL", *command]
    return command


def self_test(args: argparse.Namespace) -> int:
    problems: list[str] = []
    if shutil.which("pocketsphinx_continuous") is None:
        problems.append("pocketsphinx_continuous is not installed")
    if shutil.which("arecord") is None:
        problems.append("arecord is not installed; ALSA capture cannot be piped to PocketSphinx")
    if not Path(args.hmm).is_dir():
        problems.append(f"PocketSphinx acoustic model directory is missing: {args.hmm}")
    if not Path(args.dict_path).is_file():
        problems.append(f"PocketSphinx dictionary is missing: {args.dict_path}")
    if not args.phrase.strip():
        problems.append("wake phrase must not be empty")
    problems.extend(validate_overlap_settings(args))

    resolution: DictionaryResolution | None = None
    if not problems:
        try:
            resolution = _dictionary_resolution(args)
        except Exception as exc:
            problems.append(str(exc))

    if problems:
        print(json.dumps({"ok": False, "problems": problems}, sort_keys=True))
        return 1

    assert resolution is not None
    print(
        json.dumps(
            {
                "ok": True,
                "engine": ENGINE_NAME,
                "capture_backend": CAPTURE_BACKEND,
                "phrase": args.phrase,
                "decoder_keyphrase": _decoder_keyphrase(args.phrase),
                "device": args.device,
                "sample_rate": args.sample_rate,
                "channels": args.channels,
                "window_seconds": float(args.window_seconds),
                "hop_seconds": float(args.hop_seconds),
                "overlap_seconds": overlap_seconds(args),
                "window_bytes": window_bytes(args),
                "hop_bytes": hop_bytes(args),
                "cooldown_seconds": float(args.cooldown_seconds),
                "hmm": args.hmm,
                "base_dict": resolution.base_path,
                "dict": resolution.path,
                "custom_dict_path": resolution.custom_path,
                "custom_pronunciations_added": resolution.added_pronunciations,
                "threshold": args.threshold,
                "arecord_command": build_arecord_command(args),
                "decoder_command": build_pocketsphinx_command(args),
            },
            sort_keys=True,
        )
    )
    return 0


def _terminate_process(proc: subprocess.Popen[object], *, timeout: float = 2.0) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=timeout)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=timeout)
        except Exception:
            pass


def _terminate_children() -> None:
    global _capture_child, _decoder_child
    decoder = _decoder_child
    capture = _capture_child
    if decoder is not None:
        _terminate_process(decoder)
    if capture is not None:
        _terminate_process(capture)
    _capture_child = None
    _decoder_child = None


def _handle_signal(signum: int, frame: object | None) -> None:  # pragma: no cover - exercised by process manager
    global _stop_requested
    _stop_requested = True
    _terminate_children()


def _emit_wake_event(line: str, args: argparse.Namespace, stdout: TextIO | None = None) -> None:
    stream = stdout if stdout is not None else sys.stdout
    print(
        json.dumps(
            {
                "event": "wake",
                "engine": ENGINE_NAME,
                "capture_backend": CAPTURE_BACKEND,
                "phrase": args.phrase,
                "confidence": None,
                "raw": line,
                "timestamp_monotonic": time.monotonic(),
                "window_seconds": float(args.window_seconds),
                "hop_seconds": float(args.hop_seconds),
                "threshold": str(args.threshold),
            },
            sort_keys=True,
        ),
        file=stream,
        flush=True,
    )


def _emit_diagnostic_summary(stats: DetectionStats, args: argparse.Namespace, stdout: TextIO | None = None) -> None:
    stream = stdout if stdout is not None else sys.stdout
    dictionary_error: str | None = None
    try:
        resolution = _dictionary_resolution(args)
    except Exception as exc:  # pragma: no cover - defensive failure summary
        dictionary_error = str(exc)
        resolution = DictionaryResolution(path=str(args.dict_path), base_path=str(args.dict_path))
    print(
        json.dumps(
            {
                "event": "diagnostic_summary",
                "backend": ENGINE_NAME,
                "capture_backend": CAPTURE_BACKEND,
                "phrase": args.phrase,
                "decoder_keyphrase": _decoder_keyphrase(args.phrase),
                "chunks_processed": stats.chunks_processed,
                "detections": stats.detections,
                "last_detection_phrase": stats.last_detection_phrase,
                "last_detection_raw": stats.last_detection_raw,
                "last_raw_recognizer_line": stats.last_raw_recognizer_line,
                "last_decoder_exit_code": stats.last_decoder_exit_code,
                "threshold": str(args.threshold),
                "window_seconds": float(args.window_seconds),
                "hop_seconds": float(args.hop_seconds),
                "overlap_seconds": overlap_seconds(args),
                "cooldown_seconds": float(args.cooldown_seconds),
                "sample_rate": int(args.sample_rate),
                "channels": int(args.channels),
                "device": args.device,
                "dict": resolution.path,
                "base_dict": resolution.base_path,
                "custom_pronunciations_added": resolution.added_pronunciations,
                "dictionary_error": dictionary_error,
            },
            sort_keys=True,
        ),
        file=stream,
        flush=True,
    )


def _sleep_interruptible(seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while not _stop_requested:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 0.1))


def _should_emit_detection(last_detection_at: float | None, now: float, cooldown_seconds: float) -> bool:
    return last_detection_at is None or (now - last_detection_at) >= cooldown_seconds


class RollingAudioCapture:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.max_buffer_bytes = window_bytes(args)
        self.read_size = max(1024, min(hop_bytes(args), 8192))
        self._buffer = bytearray()
        self._total_bytes_read = 0
        self._eof = False
        self._reader_error: str | None = None
        self._condition = threading.Condition()
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        global _capture_child
        self._proc = subprocess.Popen(build_arecord_command(self.args), stdout=subprocess.PIPE, stderr=None)
        _capture_child = self._proc
        self._thread = threading.Thread(target=self._reader, name="pocketsphinx-wake-arecord-reader", daemon=True)
        self._thread.start()

    def _reader(self) -> None:
        proc = self._proc
        try:
            if proc is None or proc.stdout is None:
                raise RuntimeError("arecord stdout pipe was not created")
            while not _stop_requested:
                data = proc.stdout.read(self.read_size)
                if not data:
                    break
                with self._condition:
                    self._total_bytes_read += len(data)
                    self._buffer.extend(data)
                    excess = len(self._buffer) - self.max_buffer_bytes
                    if excess > 0:
                        del self._buffer[:excess]
                    self._condition.notify_all()
        except Exception as exc:  # pragma: no cover - defensive, surfaced through stderr/status
            with self._condition:
                self._reader_error = str(exc)
                self._condition.notify_all()
        finally:
            with self._condition:
                self._eof = True
                self._condition.notify_all()

    @property
    def total_bytes_read(self) -> int:
        with self._condition:
            return self._total_bytes_read

    @property
    def ended(self) -> bool:
        with self._condition:
            return self._eof

    @property
    def reader_error(self) -> str | None:
        with self._condition:
            return self._reader_error

    def returncode(self) -> int | None:
        proc = self._proc
        return proc.poll() if proc is not None else None

    def wait_for_total_bytes(self, target_total_bytes: int, timeout: float = 0.25) -> bool:
        with self._condition:
            while self._total_bytes_read < target_total_bytes and not self._eof and not _stop_requested:
                self._condition.wait(timeout=timeout)
            return self._total_bytes_read >= target_total_bytes

    def snapshot(self) -> bytes:
        with self._condition:
            return bytes(self._buffer[-self.max_buffer_bytes :])

    def stop(self) -> None:
        global _capture_child
        proc = self._proc
        if proc is not None:
            _terminate_process(proc)
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)
        if _capture_child is proc:
            _capture_child = None


def run_decode_window(args: argparse.Namespace, audio: bytes) -> DecodeResult:
    """Decode one finite overlapping audio snapshot and return whether the phrase was heard."""

    global _decoder_child
    if not audio:
        return DecodeResult(False, None, None, None, 0)
    decoder_command = build_pocketsphinx_command(args)
    try:
        _decoder_child = subprocess.Popen(
            decoder_command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,
        )
    except Exception as exc:
        print(f"failed to start pocketsphinx decoder process: {exc}", file=sys.stderr, flush=True)
        return DecodeResult(False, None, None, 1, len(audio))

    timeout_seconds = float(args.window_seconds) + float(args.decoder_grace_seconds)
    try:
        stdout_bytes, _ = _decoder_child.communicate(input=audio, timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        print(
            f"pocketsphinx_continuous did not finish within {timeout_seconds:.1f}s for an overlapping wake window",
            file=sys.stderr,
            flush=True,
        )
        _terminate_children()
        return DecodeResult(False, None, None, 124, len(audio))

    decoder_code = _decoder_child.returncode
    _decoder_child = None
    stdout_text = stdout_bytes.decode(errors="replace") if stdout_bytes else ""
    detected_line: str | None = None
    last_line: str | None = None
    for raw in stdout_text.splitlines():
        line = raw.strip()
        if not line:
            continue
        last_line = line
        if detected_line is None and _contains_phrase(line, args.phrase):
            detected_line = line
    return DecodeResult(
        detected=detected_line is not None,
        detected_line=detected_line,
        last_decoder_line=last_line,
        decoder_exit_code=decoder_code,
        bytes_decoded=len(audio),
    )


def _is_normal_decode_result(result: DecodeResult) -> bool:
    return result.decoder_exit_code in (0, None)


def run(args: argparse.Namespace) -> int:
    global _stop_requested
    _stop_requested = False
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    problems = validate_overlap_settings(args)
    if problems:
        for problem in problems:
            print(problem, file=sys.stderr, flush=True)
        if args.diagnostic_summary:
            _emit_diagnostic_summary(DetectionStats(), args)
        return 2
    try:
        _dictionary_resolution(args)
    except Exception as exc:
        print(f"failed to prepare PocketSphinx dictionary: {exc}", file=sys.stderr, flush=True)
        if args.diagnostic_summary:
            _emit_diagnostic_summary(DetectionStats(), args)
        return 2

    last_detection_at: float | None = None
    stats = DetectionStats()
    capture = RollingAudioCapture(args)
    exit_code = 0
    next_decode_at = window_bytes(args)
    hop = hop_bytes(args)
    try:
        try:
            capture.start()
        except Exception as exc:
            print(f"failed to start arecord capture process: {exc}", file=sys.stderr, flush=True)
            return 1

        while not _stop_requested:
            if args.max_chunks is not None and stats.chunks_processed >= args.max_chunks:
                break
            if not capture.wait_for_total_bytes(next_decode_at):
                if _stop_requested:
                    break
                if capture.reader_error:
                    print(f"arecord reader failed: {capture.reader_error}", file=sys.stderr, flush=True)
                    exit_code = 1
                    break
                if capture.ended:
                    code = capture.returncode()
                    if code not in (0, None):
                        print(f"arecord exited with code {code} during wake capture", file=sys.stderr, flush=True)
                        exit_code = int(code or 1)
                    break
                continue

            audio = capture.snapshot()
            result = run_decode_window(args, audio)
            stats.chunks_processed += 1
            stats.last_decoder_exit_code = result.decoder_exit_code
            stats.last_raw_recognizer_line = result.last_decoder_line

            if _stop_requested:
                break
            if not _is_normal_decode_result(result):
                print(
                    f"pocketsphinx_continuous exited with code {result.decoder_exit_code} during overlapping wake window",
                    file=sys.stderr,
                    flush=True,
                )
                exit_code = int(result.decoder_exit_code or 1)
                break

            if result.detected and result.detected_line:
                now = time.monotonic()
                if _should_emit_detection(last_detection_at, now, float(args.cooldown_seconds)):
                    _emit_wake_event(result.detected_line, args)
                    last_detection_at = now
                    stats.detections += 1
                    stats.last_detection_phrase = args.phrase
                    stats.last_detection_raw = result.detected_line
                    if float(args.cooldown_seconds) > 0:
                        _sleep_interruptible(float(args.cooldown_seconds))
                        next_decode_at = max(next_decode_at, capture.total_bytes_read + hop)
                        continue

            next_decode_at += hop
    finally:
        capture.stop()
        _terminate_children()
        if args.diagnostic_summary:
            _emit_diagnostic_summary(stats, args)
    return exit_code


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.self_test:
        return self_test(args)
    return run(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
