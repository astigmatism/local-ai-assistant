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
    def __init__(self, calls: list[tuple[str, Any]], event_name: str):
        self.calls = calls
        self.event_name = event_name
        self.stopped = False
        calls.append(("loop_start", event_name))
        if event_name == str(SoundEvent.THINKING):
            calls.append(("thinking_start", None))

    async def stop(self) -> None:
        self.stopped = True
        self.calls.append(("loop_stop", self.event_name))
        if self.event_name == str(SoundEvent.THINKING):
            self.calls.append(("thinking_stop", None))


class FakeAudio:
    def __init__(self, tmp_path: Path):
        self.tmp_path = tmp_path
        self.calls: list[tuple[str, Any]] = []
        self.command_texts: list[str | None] = []
        self.block_playback = False
        self.playback_started = asyncio.Event()
        self.allow_playback_finish = asyncio.Event()
        self.block_wake_ack = False
        self.fail_wake_ack = False
        self.wake_ack_started = asyncio.Event()
        self.allow_wake_ack_finish = asyncio.Event()
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

    async def play_sound_event(self, cfg, event, *, cancel_event=None, serialize=True, require_playback=False):
        event_name = str(event)
        self.calls.append(("play_sound_event", event_name))
        self.calls.append(("play_sound_event_start", event_name))
        if event_name in {str(SoundEvent.WAKE_ACK), str(SoundEvent.WAKE_NEW_CONVERSATION)}:
            self.wake_ack_started.set()
            if self.fail_wake_ack:
                self.calls.append(("play_sound_event_failed", event_name))
                raise RuntimeError("wake ack playback failed")
            if self.block_wake_ack:
                while not self.allow_wake_ack_finish.is_set():
                    if cancel_event and cancel_event.is_set():
                        self.calls.append(("play_sound_event_cancelled", event_name))
                        raise asyncio.CancelledError
                    await asyncio.sleep(0.01)
        self.calls.append(("play_sound_event_end", event_name))

    def start_looping_sound(self, cfg, event):
        event_name = str(event)
        self.calls.append(("loop_requested", event_name))
        return FakeLoopHandle(self.calls, event_name)

    async def play_file(self, cfg, path, *, cancel_event=None, require_playback=False):
        self.calls.append(("play_file", str(path)))
        if require_playback:
            self.calls.append(("play_file_require_playback", str(path)))
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
    def __init__(
        self,
        outputs: list[str] | None = None,
        exc: BaseException | None = None,
        trace: list[tuple[str, Any]] | None = None,
    ):
        self.outputs = outputs or ["hello assistant"]
        self.exc = exc
        self.calls: list[str] = []
        self.trace = trace
        self.block_calls = 0
        self.started = asyncio.Event()
        self.allow_finish = asyncio.Event()
        self.cancelled = False

    async def transcribe(self, wav_path):
        self.calls.append(str(wav_path))
        if self.trace is not None:
            self.trace.append(("stt_start", str(wav_path)))
        self.started.set()
        if self.block_calls > 0:
            self.block_calls -= 1
            try:
                while not self.allow_finish.is_set():
                    await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                self.cancelled = True
                if self.trace is not None:
                    self.trace.append(("stt_cancelled", str(wav_path)))
                raise
        if self.exc:
            if self.trace is not None:
                self.trace.append(("stt_error", str(self.exc)))
            raise self.exc
        transcript = self.outputs.pop(0) if self.outputs else ""
        if self.trace is not None:
            self.trace.append(("stt_end", transcript))
        return transcript


class FakeLLM:
    def __init__(
        self,
        outputs: list[str] | None = None,
        exc: BaseException | None = None,
        trace: list[tuple[str, Any]] | None = None,
    ):
        self.outputs = outputs or ["hello human"]
        self.exc = exc
        self.messages: list[list[dict[str, str]]] = []
        self.trace = trace
        self.block_calls = 0
        self.started = asyncio.Event()
        self.allow_finish = asyncio.Event()
        self.cancelled = False

    async def chat(self, messages):
        self.messages.append(messages)
        if self.trace is not None:
            self.trace.append(("llm_start", len(messages)))
        self.started.set()
        if self.block_calls > 0:
            self.block_calls -= 1
            try:
                while not self.allow_finish.is_set():
                    await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                self.cancelled = True
                if self.trace is not None:
                    self.trace.append(("llm_cancelled", len(messages)))
                raise
        if self.exc:
            if self.trace is not None:
                self.trace.append(("llm_error", str(self.exc)))
            raise self.exc
        output = self.outputs.pop(0) if self.outputs else "ok"
        if self.trace is not None:
            self.trace.append(("llm_end", output))
        return output


class FakeTTS:
    def __init__(self, exc: BaseException | None = None, trace: list[tuple[str, Any]] | None = None):
        self.exc = exc
        self.inputs: list[str] = []
        self.trace = trace
        self.block_calls = 0
        self.started = asyncio.Event()
        self.allow_finish = asyncio.Event()
        self.cancelled = False

    async def synthesize(self, text, output_path):
        self.inputs.append(text)
        if self.trace is not None:
            self.trace.append(("tts_start", text))
        self.started.set()
        if self.block_calls > 0:
            self.block_calls -= 1
            try:
                while not self.allow_finish.is_set():
                    await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                self.cancelled = True
                if self.trace is not None:
                    self.trace.append(("tts_cancelled", text))
                raise
        if self.exc:
            if self.trace is not None:
                self.trace.append(("tts_error", str(self.exc)))
            raise self.exc
        path = write_wav(output_path)
        if self.trace is not None:
            self.trace.append(("tts_end", str(path)))
        return path


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
    cfg["command_registry"]["recognizer"]["engine"] = "configured_text"
    os.makedirs(cfg["sounds"]["library_dir"], exist_ok=True)
    configured_sound_names = []
    for value in cfg["sounds"]["event_files"].values():
        if isinstance(value, list):
            configured_sound_names.extend(value)
        else:
            configured_sound_names.append(value)
    for name in {name for name in configured_sound_names if name}:
        write_wav(Path(cfg["sounds"]["library_dir"]) / name)
    store.apply_config(cfg)
    telemetry = TelemetryStore(cfg["storage"]["telemetry_db_path"], cfg["storage"]["artifacts_dir"])
    audio = FakeAudio(tmp_path)
    stt = FakeSTT(trace=audio.calls)
    llm = FakeLLM(trace=audio.calls)
    tts = FakeTTS(trace=audio.calls)
    runtime = AssistantRuntime(
        store,
        telemetry,
        audio=audio,
        stt_factory=lambda c: stt,
        llm_factory=lambda c: llm,
        tts_factory=lambda c: tts,
    )
    return store, telemetry, runtime, audio, stt, llm, tts
