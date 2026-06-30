from __future__ import annotations

import abc
import asyncio
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from .config import AssistantConfig


@dataclass(frozen=True)
class WakeDetection:
    phrase: str
    confidence: float | None
    engine: str
    timestamp_monotonic: float


WakeCallback = Callable[[WakeDetection], Awaitable[None]]


class WakeWordEngine(abc.ABC):
    @abc.abstractmethod
    async def run(self, callback: WakeCallback, stop_event: asyncio.Event) -> None:
        raise NotImplementedError


class SimulatedWakeWordEngine(WakeWordEngine):
    """Test/admin wake source. It does not replace production local wake detection."""

    def __init__(self, phrase: str):
        self.phrase = phrase
        self.queue: asyncio.Queue[WakeDetection] = asyncio.Queue()

    async def trigger(self, confidence: float = 1.0, phrase: str | None = None) -> WakeDetection:
        detection = WakeDetection(
            phrase=phrase or self.phrase,
            confidence=confidence,
            engine="simulated",
            timestamp_monotonic=time.monotonic(),
        )
        await self.queue.put(detection)
        return detection

    async def run(self, callback: WakeCallback, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                detection = await asyncio.wait_for(self.queue.get(), timeout=0.2)
            except asyncio.TimeoutError:
                continue
            await callback(detection)


class ExternalCommandWakeWordEngine(WakeWordEngine):
    """Runs a local dedicated wake-word tool and treats each stdout line as a wake event.

    This supports engines that are packaged outside Python. It is still local thin-client detection;
    it does not use the downstream speech-to-text service.
    """

    def __init__(self, command: list[str], phrase: str):
        if not command:
            raise ValueError("external_command wake engine requires a command")
        self.command = command
        self.phrase = phrase

    async def run(self, callback: WakeCallback, stop_event: asyncio.Event) -> None:
        proc = await asyncio.create_subprocess_exec(
            *self.command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            while not stop_event.is_set():
                try:
                    line = await asyncio.wait_for(proc.stdout.readline(), timeout=0.2)  # type: ignore[union-attr]
                except asyncio.TimeoutError:
                    continue
                if not line:
                    break
                text = line.decode(errors="replace").strip()
                confidence = None
                if "confidence=" in text:
                    try:
                        confidence = float(text.split("confidence=", 1)[1].split()[0])
                    except ValueError:
                        confidence = None
                await callback(
                    WakeDetection(
                        phrase=self.phrase,
                        confidence=confidence,
                        engine="external_command",
                        timestamp_monotonic=time.monotonic(),
                    )
                )
        finally:
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=1)
                except asyncio.TimeoutError:
                    proc.kill()


class OpenWakeWordEngine(WakeWordEngine):
    """Local openWakeWord adapter fed from ALSA raw microphone audio."""

    def __init__(self, cfg: AssistantConfig):
        self.cfg = cfg

    async def run(self, callback: WakeCallback, stop_event: asyncio.Event) -> None:  # pragma: no cover - hardware/optional dependency
        if shutil.which("arecord") is None:
            raise RuntimeError("arecord is required for openWakeWord audio capture")
        try:
            import numpy as np  # type: ignore
            from openwakeword.model import Model  # type: ignore
        except Exception as exc:
            raise RuntimeError("openwakeword and numpy are required for wake.engine=openwakeword") from exc

        model_kwargs = {}
        if self.cfg.wake.model_path:
            model_kwargs["wakeword_models"] = [self.cfg.wake.model_path]
        model = Model(**model_kwargs)
        chunk_samples = 1280
        chunk_bytes = chunk_samples * 2
        proc = await asyncio.create_subprocess_exec(
            "arecord",
            "-q",
            "-D",
            self.cfg.audio.capture_device,
            "-f",
            "S16_LE",
            "-r",
            "16000",
            "-c",
            "1",
            "-t",
            "raw",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            while not stop_event.is_set():
                try:
                    data = await asyncio.wait_for(proc.stdout.readexactly(chunk_bytes), timeout=1.0)  # type: ignore[union-attr]
                except asyncio.TimeoutError:
                    continue
                except asyncio.IncompleteReadError:
                    break
                audio = np.frombuffer(data, dtype=np.int16)
                scores = model.predict(audio)
                if not scores:
                    continue
                phrase, score = max(scores.items(), key=lambda item: item[1])
                if float(score) >= self.cfg.wake.sensitivity:
                    await callback(
                        WakeDetection(
                            phrase=self.cfg.wake.active_wake_phrase or phrase,
                            confidence=float(score),
                            engine="openwakeword",
                            timestamp_monotonic=time.monotonic(),
                        )
                    )
                    # Small debounce to avoid repeated detections from the same utterance.
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=1.0)
                    except asyncio.TimeoutError:
                        pass
        finally:
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=1)
                except asyncio.TimeoutError:
                    proc.kill()


def build_wake_engine(cfg: AssistantConfig) -> WakeWordEngine:
    if cfg.wake.engine == "openwakeword":
        return OpenWakeWordEngine(cfg)
    if cfg.wake.engine == "external_command":
        return ExternalCommandWakeWordEngine(cfg.wake.external_command, cfg.wake.active_wake_phrase)
    return SimulatedWakeWordEngine(cfg.wake.active_wake_phrase)
