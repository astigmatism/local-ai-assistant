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
from typing import Iterable

DEFAULT_HMM_DIR = "/usr/share/pocketsphinx/model/en-us/en-us"
DEFAULT_DICT_PATH = "/usr/share/pocketsphinx/model/en-us/cmudict-en-us.dict"
DEFAULT_DEVICE = "plughw:0,0"
DEFAULT_SAMPLE_RATE = 16000
DEFAULT_PHRASE = "computer"

_WORD_RE = re.compile(r"[a-z0-9']+")
_child: subprocess.Popen[str] | None = None


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


def self_test(args: argparse.Namespace) -> int:
    problems: list[str] = []
    if shutil.which("pocketsphinx_continuous") is None:
        problems.append("pocketsphinx_continuous is not installed")
    if shutil.which("arecord") is None:
        problems.append("arecord is not installed; ALSA capture diagnostics will be unavailable")
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
                "phrase": args.phrase,
                "device": args.device,
                "sample_rate": args.sample_rate,
                "hmm": args.hmm,
                "dict": args.dict_path,
                "threshold": args.threshold,
            },
            sort_keys=True,
        )
    )
    return 0


def build_pocketsphinx_command(args: argparse.Namespace) -> list[str]:
    return [
        "pocketsphinx_continuous",
        "-inmic",
        "yes",
        "-adcdev",
        args.device,
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


def _terminate_child() -> None:
    global _child
    child = _child
    if child and child.poll() is None:
        try:
            child.terminate()
            child.wait(timeout=2)
        except Exception:
            try:
                child.kill()
            except Exception:
                pass
    _child = None


def _handle_signal(signum: int, frame: object | None) -> None:  # pragma: no cover - exercised by process manager
    _terminate_child()
    raise SystemExit(0)


def run(args: argparse.Namespace) -> int:
    global _child
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    command = build_pocketsphinx_command(args)
    _child = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )
    try:
        assert _child.stdout is not None
        for raw in _child.stdout:
            line = raw.strip()
            if not line:
                continue
            if not _contains_phrase(line, args.phrase):
                continue
            print(
                json.dumps(
                    {
                        "event": "wake",
                        "engine": "pocketsphinx_continuous",
                        "phrase": args.phrase,
                        "confidence": None,
                        "raw": line,
                        "timestamp_monotonic": time.monotonic(),
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
        return _child.wait()
    finally:
        _terminate_child()


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.self_test:
        return self_test(args)
    return run(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
