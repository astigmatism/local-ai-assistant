from __future__ import annotations

import asyncio
import math
import os
import wave
from pathlib import Path
from typing import Any

import pytest

from voice_assistant.assistant import AssistantRuntime
from voice_assistant.audio import CaptureResult
from voice_assistant.config import ConfigStore
from voice_assistant.constants import SoundEvent
from voice_assistant.telemetry import TelemetryStore


def write_wav(path: str | Path, duration: float = 0.02, sr: int = 16000) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sr)
        frames = []
        for i in range(max(1, int(sr * duration))):
            sample = int(math.sin(2 * math.pi * 440 * i / sr) * 1200)
            frames.append(sample.to_bytes(2, "little", signed=True))
        wav.writeframes(b"".join(frames))
    return path


class FakeLoopHandle:
    def __init__(self, calls: list[tuple[str, Any]]):
        self.calls = calls
        self.stopped = False
        calls.append(("thinking_start", None))

    async def stop(self) -> None:
        self.stopped = True
        self.calls.append(("thinking_stop", None))


class FakeAudio:
    def __init__(self, tmp_path: Path):
        self.tmp_path = tmp_path
        self.calls: list[tuple[str, Any]] = []
        self.command_texts: list[str | None] = []
        self.block_playback = False
        self.playback_started = asyncio.Event()
        self.allow_playback_finish = asyncio.Event()
        self.stop_called = False

    def new_prompt_path(self, cfg, interaction_id=None):
        return self.tmp_path / f"prompt-{interaction_id or 'x'}.wav"

    def new_tts_path(self, cfg, interaction_id=None):
        return self.tmp_path / f"tts-{interaction_id or 'x'}.wav"

    async def record_prompt(self, cfg, output_path, cancel_event=None):
        self.calls.append(("record_prompt_start", str(output_path)))
        write_wav(output_path)
        text = self.command_texts.pop(0) if self.command_texts else None
        if text is not None:
            Path(str(output_path) + ".command.txt").write_text(text, encoding="utf-8")
        self.calls.append(("record_prompt_end", text))
        return CaptureResult(str(output_path), 0.05, "silence", 100.0, Path(output_path).stat().st_size)

    async def record_fixed_duration(self, cfg, duration_seconds, output_path):
        self.calls.append(("record_fixed_duration", duration_seconds))
        write_wav(output_path)
        return CaptureResult(str(output_path), duration_seconds, "duration", 0.0, Path(output_path).stat().st_size)

    async def play_sound_event(self, cfg, event, *, cancel_event=None, serialize=True):
        self.calls.append(("play_sound_event", str(event)))

    def start_looping_sound(self, cfg, event):
        self.calls.append(("loop_requested", str(event)))
        return FakeLoopHandle(self.calls)

    async def play_file(self, cfg, path, *, cancel_event=None):
        self.calls.append(("play_file", str(path)))
        if self.block_playback:
            self.playback_started.set()
            while not self.allow_playback_finish.is_set():
                if cancel_event and cancel_event.is_set():
                    self.calls.append(("play_file_cancelled", str(path)))
                    raise asyncio.CancelledError
                await asyncio.sleep(0.01)

    async def stop_all_playback(self):
        self.stop_called = True
        self.calls.append(("stop_all_playback", None))
        self.allow_playback_finish.set()


class FakeSTT:
    def __init__(self, outputs: list[str] | None = None, exc: BaseException | None = None):
        self.outputs = outputs or ["hello assistant"]
        self.exc = exc
        self.calls: list[str] = []

    async def transcribe(self, wav_path):
        self.calls.append(str(wav_path))
        if self.exc:
            raise self.exc
        return self.outputs.pop(0) if self.outputs else ""


class FakeLLM:
    def __init__(self, outputs: list[str] | None = None, exc: BaseException | None = None):
        self.outputs = outputs or ["hello human"]
        self.exc = exc
        self.messages: list[list[dict[str, str]]] = []

    async def chat(self, messages):
        self.messages.append(messages)
        if self.exc:
            raise self.exc
        return self.outputs.pop(0) if self.outputs else "ok"


class FakeTTS:
    def __init__(self, exc: BaseException | None = None):
        self.exc = exc
        self.inputs: list[str] = []

    async def synthesize(self, text, output_path):
        self.inputs.append(text)
        if self.exc:
            raise self.exc
        return write_wav(output_path)


@pytest.fixture
def bundle_parts(tmp_path, monkeypatch):
    monkeypatch.setenv("WHISPER_API_KEY", "test-whisper")
    monkeypatch.setenv("TTS_ROUTER_API_KEY", "test-tts")
    config_path = tmp_path / "config.json"
    store = ConfigStore(config_path)
    cfg = store.get_active().public_dict()
    cfg["storage"]["base_dir"] = str(tmp_path)
    cfg["storage"]["artifacts_dir"] = str(tmp_path / "artifacts")
    cfg["storage"]["telemetry_db_path"] = str(tmp_path / "telemetry.sqlite3")
    cfg["sounds"]["library_dir"] = str(tmp_path / "sounds")
    os.makedirs(cfg["sounds"]["library_dir"], exist_ok=True)
    for name in {v for v in cfg["sounds"]["event_files"].values()}:
        write_wav(Path(cfg["sounds"]["library_dir"]) / name)
    store.apply_config(cfg)
    telemetry = TelemetryStore(cfg["storage"]["telemetry_db_path"], cfg["storage"]["artifacts_dir"])
    audio = FakeAudio(tmp_path)
    stt = FakeSTT()
    llm = FakeLLM()
    tts = FakeTTS()
    runtime = AssistantRuntime(
        store,
        telemetry,
        audio=audio,
        stt_factory=lambda c: stt,
        llm_factory=lambda c: llm,
        tts_factory=lambda c: tts,
    )
    return store, telemetry, runtime, audio, stt, llm, tts
