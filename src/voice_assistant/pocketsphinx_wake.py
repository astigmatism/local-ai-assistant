from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, TextIO

DEFAULT_HMM_DIR = "/usr/share/pocketsphinx/model/en-us/en-us"
DEFAULT_DICT_PATH = "/usr/share/pocketsphinx/model/en-us/cmudict-en-us.dict"
DEFAULT_DEVICE = "plughw:0,0"
DEFAULT_SAMPLE_RATE = 16000
DEFAULT_CHANNELS = 1
DEFAULT_PHRASE = "computer"
DEFAULT_CHUNK_SECONDS = 4.0
DEFAULT_COOLDOWN_SECONDS = 1.5
DEFAULT_DECODER_GRACE_SECONDS = 5.0
ENGINE_NAME = "pocketsphinx_continuous_arecord_chunk"
CAPTURE_BACKEND = "arecord_chunk"

_WORD_RE = re.compile(r"[a-z0-9']+")
_capture_child: subprocess.Popen[bytes] | None = None
_decoder_child: subprocess.Popen[str] | None = None
_stop_requested = False


@dataclass(frozen=True)
class ChunkResult:
    detected: bool
    raw_line: str | None
    arecord_exit_code: int | None
    decoder_exit_code: int | None


def _normalise(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


def _contains_phrase(line: str, phrase: str) -> bool:
    phrase_words = _normalise(phrase)
    line_words = _normalise(line)
    if not phrase_words or not line_words:
        return False
    width = len(phrase_words)
    return any(line_words[index : index + width] == phrase_words for index in range(0, len(line_words) - width + 1))


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
    parser.add_argument("--phrase", default=os.getenv("VOICE_ASSISTANT_WAKE_PHRASE", DEFAULT_PHRASE))
    parser.add_argument("--device", default=os.getenv("VOICE_ASSISTANT_CAPTURE_DEVICE", DEFAULT_DEVICE))
    parser.add_argument("--sample-rate", type=_positive_int, default=_env_int("VOICE_ASSISTANT_SAMPLE_RATE_HZ", DEFAULT_SAMPLE_RATE))
    parser.add_argument("--channels", type=_positive_int, default=_env_int("VOICE_ASSISTANT_CHANNELS", DEFAULT_CHANNELS))
    parser.add_argument(
        "--chunk-seconds",
        type=_positive_float,
        default=_env_float("VOICE_ASSISTANT_WAKE_CHUNK_SECONDS", DEFAULT_CHUNK_SECONDS),
        help="finite ALSA capture window length; arecord receives the rounded whole-second duration",
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
        help="extra time allowed for PocketSphinx to finish after each finite capture window",
    )
    parser.add_argument("--hmm", default=os.getenv("VOICE_ASSISTANT_WAKE_MODEL_PATH", DEFAULT_HMM_DIR))
    parser.add_argument("--dict", dest="dict_path", default=os.getenv("VOICE_ASSISTANT_POCKETSPHINX_DICT", DEFAULT_DICT_PATH))
    parser.add_argument(
        "--threshold",
        default=os.getenv(
            "VOICE_ASSISTANT_POCKETSPHINX_THRESHOLD",
            threshold_from_sensitivity(_env_float("VOICE_ASSISTANT_WAKE_SENSITIVITY", 0.5)),
        ),
    )
    parser.add_argument("--self-test", action="store_true", help="Validate local wake prerequisites and exit")
    parser.add_argument(
        "--max-chunks",
        type=_positive_int,
        default=None,
        help="debug/test only: exit after processing this many finite wake windows",
    )
    return parser


def _arecord_duration_seconds(args: argparse.Namespace) -> int:
    return max(1, int(round(float(args.chunk_seconds))))


def build_arecord_command(args: argparse.Namespace) -> list[str]:
    """Build the finite ALSA capture command used for production wake audio.

    We intentionally capture with ``arecord`` instead of PocketSphinx's ``-inmic yes`` live
    microphone backend. On the target thin client ``arecord -D plughw:0,0`` can open the EMEET
    microphone reliably while ``pocketsphinx_continuous -inmic yes -adcdev plughw:0,0`` exits with
    "Connection refused". Each invocation captures a short finite raw PCM window so
    ``pocketsphinx_continuous -infile /dev/stdin`` receives EOF and performs keyword detection.
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
        "-d",
        str(_arecord_duration_seconds(args)),
    ]


def build_pocketsphinx_command(args: argparse.Namespace) -> list[str]:
    command = [
        "pocketsphinx_continuous",
        "-infile",
        "/dev/stdin",
        "-samprate",
        str(args.sample_rate),
        "-hmm",
        args.hmm,
        "-dict",
        args.dict_path,
        "-keyphrase",
        args.phrase,
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
    if args.channels != 1:
        problems.append("PocketSphinx wake is packaged and tested for mono capture; set channels to 1")
    if problems:
        print(json.dumps({"ok": False, "problems": problems}, sort_keys=True))
        return 1
    print(
        json.dumps(
            {
                "ok": True,
                "engine": ENGINE_NAME,
                "capture_backend": CAPTURE_BACKEND,
                "phrase": args.phrase,
                "device": args.device,
                "sample_rate": args.sample_rate,
                "channels": args.channels,
                "chunk_seconds": float(args.chunk_seconds),
                "arecord_duration_seconds": _arecord_duration_seconds(args),
                "cooldown_seconds": float(args.cooldown_seconds),
                "hmm": args.hmm,
                "dict": args.dict_path,
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
    if capture is not None:
        _terminate_process(capture)
    if decoder is not None:
        _terminate_process(decoder)
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


def run_detection_window(args: argparse.Namespace) -> ChunkResult:
    """Run one finite arecord -> PocketSphinx window and return whether the phrase was heard."""

    global _capture_child, _decoder_child
    capture_command = build_arecord_command(args)
    decoder_command = build_pocketsphinx_command(args)
    detected_line: str | None = None
    try:
        _capture_child = subprocess.Popen(
            capture_command,
            stdout=subprocess.PIPE,
            stderr=None,
        )
    except Exception as exc:
        print(f"failed to start arecord capture process: {exc}", file=sys.stderr, flush=True)
        return ChunkResult(False, None, 1, None)
    if _capture_child.stdout is None:  # pragma: no cover - defensive
        print("arecord stdout pipe was not created", file=sys.stderr, flush=True)
        _terminate_children()
        return ChunkResult(False, None, 1, None)

    try:
        _decoder_child = subprocess.Popen(
            decoder_command,
            stdin=_capture_child.stdout,
            stdout=subprocess.PIPE,
            stderr=None,
            text=True,
        )
    except Exception as exc:
        print(f"failed to start pocketsphinx decoder process: {exc}", file=sys.stderr, flush=True)
        _terminate_children()
        return ChunkResult(False, None, None, 1)
    # Let the decoder own the read end. This allows arecord to receive SIGPIPE/EOF correctly if the
    # decoder exits, instead of keeping the pipe alive in this parent process.
    _capture_child.stdout.close()

    timeout_seconds = _arecord_duration_seconds(args) + float(args.decoder_grace_seconds)
    decoder_stdout = ""
    try:
        decoder_stdout, _ = _decoder_child.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        print(
            f"pocketsphinx_continuous did not finish within {timeout_seconds:.1f}s for a finite wake chunk",
            file=sys.stderr,
            flush=True,
        )
        _terminate_children()
        return ChunkResult(False, None, 1, 124)

    try:
        capture_code: int | None = _capture_child.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        print("arecord did not exit after finite wake chunk", file=sys.stderr, flush=True)
        _terminate_children()
        return ChunkResult(False, None, 124, _decoder_child.returncode if _decoder_child else None)

    decoder_code = _decoder_child.returncode
    for raw in decoder_stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        if _contains_phrase(line, args.phrase):
            detected_line = line
            break

    _capture_child = None
    _decoder_child = None
    return ChunkResult(
        detected=detected_line is not None,
        raw_line=detected_line,
        arecord_exit_code=capture_code,
        decoder_exit_code=decoder_code,
    )


def _is_normal_chunk_result(result: ChunkResult) -> bool:
    return (result.decoder_exit_code in (0, None)) and (result.arecord_exit_code in (0, None))


def run(args: argparse.Namespace) -> int:
    global _stop_requested
    _stop_requested = False
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    last_detection_at: float | None = None
    chunks_processed = 0
    try:
        while not _stop_requested:
            result = run_detection_window(args)
            chunks_processed += 1

            if _stop_requested:
                return 0
            if not _is_normal_chunk_result(result):
                if result.decoder_exit_code not in (0, None):
                    print(
                        f"pocketsphinx_continuous exited with code {result.decoder_exit_code} during wake chunk",
                        file=sys.stderr,
                        flush=True,
                    )
                    return int(result.decoder_exit_code or 1)
                if result.arecord_exit_code not in (0, None):
                    print(f"arecord exited with code {result.arecord_exit_code} during wake chunk", file=sys.stderr, flush=True)
                    return int(result.arecord_exit_code or 1)

            if result.detected and result.raw_line:
                now = time.monotonic()
                if _should_emit_detection(last_detection_at, now, float(args.cooldown_seconds)):
                    _emit_wake_event(result.raw_line, args)
                    last_detection_at = now
                    if float(args.cooldown_seconds) > 0:
                        _sleep_interruptible(float(args.cooldown_seconds))

            if args.max_chunks is not None and chunks_processed >= args.max_chunks:
                return 0
    finally:
        _terminate_children()
    return 0


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.self_test:
        return self_test(args)
    return run(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
