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
from pathlib import Path
from typing import Iterable, TextIO

DEFAULT_HMM_DIR = "/usr/share/pocketsphinx/model/en-us/en-us"
DEFAULT_DICT_PATH = "/usr/share/pocketsphinx/model/en-us/cmudict-en-us.dict"
DEFAULT_DEVICE = "plughw:0,0"
DEFAULT_SAMPLE_RATE = 16000
DEFAULT_PHRASE = "computer"

_WORD_RE = re.compile(r"[a-z0-9']+")
_capture_child: subprocess.Popen[bytes] | None = None
_decoder_child: subprocess.Popen[str] | None = None


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local PocketSphinx wake-word stdout adapter")
    parser.add_argument("--phrase", default=os.getenv("VOICE_ASSISTANT_WAKE_PHRASE", DEFAULT_PHRASE))
    parser.add_argument("--device", default=os.getenv("VOICE_ASSISTANT_CAPTURE_DEVICE", DEFAULT_DEVICE))
    parser.add_argument("--sample-rate", type=int, default=int(os.getenv("VOICE_ASSISTANT_SAMPLE_RATE_HZ", str(DEFAULT_SAMPLE_RATE))))
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
    return parser


def build_arecord_command(args: argparse.Namespace) -> list[str]:
    """Build the ALSA capture command used for production wake audio.

    We intentionally capture with ``arecord`` instead of PocketSphinx's ``-inmic yes`` PortAudio-like
    live microphone backend. On the target thin client ``arecord -D plughw:0,0`` can open the EMEET
    microphone reliably while ``pocketsphinx_continuous -inmic yes -adcdev plughw:0,0`` exits with
    "Connection refused". The decoder therefore receives raw 16 kHz mono PCM through stdin.
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
        "1",
        "-t",
        "raw",
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
    if problems:
        print(json.dumps({"ok": False, "problems": problems}, sort_keys=True))
        return 1
    print(
        json.dumps(
            {
                "ok": True,
                "engine": "pocketsphinx_continuous",
                "capture_backend": "arecord_pipe",
                "phrase": args.phrase,
                "device": args.device,
                "sample_rate": args.sample_rate,
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
    # Stop capture first so the decoder gets EOF, then stop the decoder if it did not exit.
    if capture is not None:
        _terminate_process(capture)
    if decoder is not None:
        _terminate_process(decoder)
    _capture_child = None
    _decoder_child = None


def _handle_signal(signum: int, frame: object | None) -> None:  # pragma: no cover - exercised by process manager
    _terminate_children()
    raise SystemExit(0)


def _emit_wake_event(line: str, args: argparse.Namespace, stdout: TextIO | None = None) -> None:
    stream = stdout if stdout is not None else sys.stdout
    print(
        json.dumps(
            {
                "event": "wake",
                "engine": "pocketsphinx_continuous_arecord_pipe",
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


def run(args: argparse.Namespace) -> int:
    global _capture_child, _decoder_child
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    capture_command = build_arecord_command(args)
    decoder_command = build_pocketsphinx_command(args)
    try:
        _capture_child = subprocess.Popen(
            capture_command,
            stdout=subprocess.PIPE,
            stderr=None,
        )
    except Exception as exc:
        print(f"failed to start arecord capture process: {exc}", file=sys.stderr, flush=True)
        return 1
    if _capture_child.stdout is None:  # pragma: no cover - defensive
        print("arecord stdout pipe was not created", file=sys.stderr, flush=True)
        _terminate_children()
        return 1

    try:
        _decoder_child = subprocess.Popen(
            decoder_command,
            stdin=_capture_child.stdout,
            stdout=subprocess.PIPE,
            stderr=None,
            text=True,
            bufsize=1,
        )
    except Exception as exc:
        print(f"failed to start pocketsphinx decoder process: {exc}", file=sys.stderr, flush=True)
        _terminate_children()
        return 1
    # Let the decoder own the read end. This allows arecord to receive SIGPIPE/EOF correctly if the
    # decoder exits, instead of keeping the pipe alive in this parent process.
    _capture_child.stdout.close()

    try:
        assert _decoder_child.stdout is not None
        for raw in _decoder_child.stdout:
            line = raw.strip()
            if not line:
                continue
            if not _contains_phrase(line, args.phrase):
                continue
            _emit_wake_event(line, args)

        decoder_code = _decoder_child.wait()
        capture_code = _capture_child.poll()
        if capture_code is None:
            _terminate_process(_capture_child)
            capture_code = _capture_child.poll()
        if decoder_code not in (0, None):
            print(f"pocketsphinx_continuous exited with code {decoder_code}", file=sys.stderr, flush=True)
            return int(decoder_code)
        if capture_code not in (0, None):
            print(f"arecord exited with code {capture_code}", file=sys.stderr, flush=True)
            return int(capture_code)
        return 0
    finally:
        _terminate_children()


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.self_test:
        return self_test(args)
    return run(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
