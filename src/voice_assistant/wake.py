from __future__ import annotations

import abc
import asyncio
import contextlib
import json
import os
import shutil
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

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

    async def pause(self, reason: str = "") -> None:
        """Temporarily release microphone resources while prompt capture owns ALSA."""

    async def resume(self) -> None:
        """Resume wake listening after prompt capture has released ALSA."""

    def status(self) -> dict[str, Any]:
        return {"engine": self.__class__.__name__}


class SimulatedWakeWordEngine(WakeWordEngine):
    """Test/admin wake source. It does not replace production local wake detection."""

    def __init__(self, phrase: str):
        self.phrase = phrase
        self.queue: asyncio.Queue[WakeDetection] = asyncio.Queue()
        self._trigger_count = 0
        self._last_detection: dict[str, Any] | None = None

    async def trigger(self, confidence: float = 1.0, phrase: str | None = None) -> WakeDetection:
        detection = WakeDetection(
            phrase=phrase or self.phrase,
            confidence=confidence,
            engine="simulated",
            timestamp_monotonic=time.monotonic(),
        )
        self._trigger_count += 1
        self._last_detection = detection.__dict__.copy()
        await self.queue.put(detection)
        return detection

    async def run(self, callback: WakeCallback, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                detection = await asyncio.wait_for(self.queue.get(), timeout=0.2)
            except asyncio.TimeoutError:
                continue
            await callback(detection)

    def status(self) -> dict[str, Any]:
        return {
            "engine": "simulated",
            "mode": "diagnostics_only",
            "production_ready": False,
            "input_source": "admin/test queue",
            "trigger_count": self._trigger_count,
            "last_detection": self._last_detection,
            "admin_test_endpoint_available": True,
        }


class ExternalCommandWakeWordEngine(WakeWordEngine):
    """Runs a local wake-word process and treats each stdout wake line as a wake event.

    The bundled production command is ``python -m voice_assistant.pocketsphinx_wake``. It runs
    ``arecord`` against the configured ALSA capture device, pipes raw 16 kHz mono PCM into
    ``pocketsphinx_continuous -infile /dev/stdin``, and emits JSON wake events on stdout.
    The adapter also preserves the legacy contract that non-empty stdout lines are wake events,
    so custom external detectors can still be used if they write detections only to stdout and
    diagnostic logs to stderr. Recent stderr lines are captured in status for deployment debugging.
    """

    def __init__(
        self,
        command: list[str],
        phrase: str,
        *,
        sensitivity: float = 0.5,
        capture_device: str = "plughw:0,0",
        sample_rate_hz: int = 16000,
        model_path: str | None = None,
    ):
        if not command:
            raise ValueError("external_command wake engine requires a command")
        self.command = command
        self.phrase = phrase
        self.sensitivity = sensitivity
        self.capture_device = capture_device
        self.sample_rate_hz = sample_rate_hz
        self.model_path = model_path
        self._pause_event = asyncio.Event()
        self._current_process: asyncio.subprocess.Process | None = None
        self._process_running = False
        self._paused_reason: str | None = None
        self._restart_count = 0
        self._detection_count = 0
        self._last_detection: dict[str, Any] | None = None
        self._last_raw_line: str | None = None
        self._last_error: str | None = None
        self._last_stderr_line: str | None = None
        self._stderr_tail: list[str] = []
        self._last_exit_code: int | None = None
        self._pid: int | None = None

    def _environment(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            {
                "VOICE_ASSISTANT_WAKE_PHRASE": self.phrase,
                "VOICE_ASSISTANT_WAKE_SENSITIVITY": str(self.sensitivity),
                "VOICE_ASSISTANT_CAPTURE_DEVICE": self.capture_device,
                "VOICE_ASSISTANT_SAMPLE_RATE_HZ": str(self.sample_rate_hz),
            }
        )
        if self.model_path:
            env["VOICE_ASSISTANT_WAKE_MODEL_PATH"] = self.model_path
        return env

    async def pause(self, reason: str = "") -> None:
        self._paused_reason = reason or "paused"
        self._pause_event.set()
        proc = self._current_process
        if proc and proc.returncode is None:
            await self._terminate(proc)

    async def resume(self) -> None:
        self._paused_reason = None
        self._pause_event.clear()

    async def run(self, callback: WakeCallback, stop_event: asyncio.Event) -> None:
        backoff_seconds = 0.5
        while not stop_event.is_set():
            if self._pause_event.is_set():
                self._process_running = False
                await self._wait_until_resumed_or_stopped(stop_event)
                continue
            try:
                proc = await asyncio.create_subprocess_exec(
                    *self.command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=self._environment(),
                )
            except Exception as exc:
                self._last_error = f"failed to start external wake command: {exc}"
                self._last_exit_code = None
                self._process_running = False
                await self._sleep_or_stop(stop_event, backoff_seconds)
                backoff_seconds = min(5.0, backoff_seconds * 2)
                continue

            self._current_process = proc
            self._process_running = True
            self._pid = proc.pid
            self._last_error = None
            self._last_stderr_line = None
            self._stderr_tail = []
            backoff_seconds = 0.5
            stderr_task = asyncio.create_task(self._read_stderr(proc))
            try:
                while not stop_event.is_set() and not self._pause_event.is_set():
                    try:
                        line = await asyncio.wait_for(proc.stdout.readline(), timeout=0.2)  # type: ignore[union-attr]
                    except asyncio.TimeoutError:
                        if proc.returncode is not None:
                            break
                        continue
                    if not line:
                        break
                    text = line.decode(errors="replace").strip()
                    detection = self.parse_detection_line(text)
                    if detection is None:
                        continue
                    self._record_detection(detection, text)
                    await callback(detection)
            finally:
                await self._terminate(proc)
                await self._finish_stderr_reader(stderr_task)
                self._process_running = False
                self._current_process = None
                self._pid = None

            if stop_event.is_set():
                break
            if self._pause_event.is_set():
                continue
            self._last_exit_code = proc.returncode
            detail = f"external wake command exited with code {proc.returncode}"
            if self._last_stderr_line:
                detail = f"{detail}: {self._last_stderr_line}"
            self._last_error = detail
            self._restart_count += 1
            await self._sleep_or_stop(stop_event, backoff_seconds)
            backoff_seconds = min(5.0, backoff_seconds * 2)

    async def _read_stderr(self, proc: asyncio.subprocess.Process) -> None:
        if proc.stderr is None:
            return
        while True:
            line = await proc.stderr.readline()
            if not line:
                return
            text = line.decode(errors="replace").strip()
            if not text:
                continue
            self._last_stderr_line = text[:500]
            self._stderr_tail.append(text[:500])
            self._stderr_tail = self._stderr_tail[-10:]

    async def _finish_stderr_reader(self, task: asyncio.Task[None]) -> None:
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=0.5)
        except asyncio.TimeoutError:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def parse_detection_line(self, text: str) -> WakeDetection | None:
        if not text:
            return None
        self._last_raw_line = text[:500]
        phrase = self.phrase
        confidence: float | None = None
        engine = "external_command"
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            event = str(payload.get("event", "wake")).lower()
            if event not in {"wake", "wake_detected", "detected"}:
                return None
            phrase_value = payload.get("phrase")
            if isinstance(phrase_value, str) and phrase_value.strip():
                phrase = phrase_value.strip()
            confidence = _coerce_float(payload.get("confidence"))
            source = payload.get("engine") or payload.get("source")
            if isinstance(source, str) and source.strip():
                engine = f"external_command:{source.strip()}"
        else:
            confidence = _parse_confidence_token(text)
            if "engine=" in text:
                engine_value = text.split("engine=", 1)[1].split()[0]
                if engine_value:
                    engine = f"external_command:{engine_value}"
        return WakeDetection(
            phrase=phrase,
            confidence=confidence,
            engine=engine,
            timestamp_monotonic=time.monotonic(),
        )

    def _record_detection(self, detection: WakeDetection, raw_line: str) -> None:
        self._detection_count += 1
        self._last_detection = {
            "phrase": detection.phrase,
            "confidence": detection.confidence,
            "engine": detection.engine,
            "timestamp_monotonic": detection.timestamp_monotonic,
            "raw": raw_line[:500],
        }

    async def _wait_until_resumed_or_stopped(self, stop_event: asyncio.Event) -> None:
        while self._pause_event.is_set() and not stop_event.is_set():
            await self._sleep_or_stop(stop_event, 0.1)

    async def _sleep_or_stop(self, stop_event: asyncio.Event, seconds: float) -> None:
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _terminate(self, proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()

    def status(self) -> dict[str, Any]:
        executable = self.command[0] if self.command else ""
        return {
            "engine": "external_command",
            "mode": "production_local_subprocess",
            "production_ready": True,
            "input_source": "local microphone via external wake process",
            "command": list(self.command),
            "executable_available": bool(executable and shutil.which(executable)),
            "process_running": self._process_running,
            "pid": self._pid,
            "paused": self._pause_event.is_set(),
            "paused_reason": self._paused_reason,
            "restart_count": self._restart_count,
            "detection_count": self._detection_count,
            "last_detection": self._last_detection,
            "last_raw_line": self._last_raw_line,
            "last_stderr_line": self._last_stderr_line,
            "stderr_tail": list(self._stderr_tail),
            "last_exit_code": self._last_exit_code,
            "last_error": self._last_error,
            "admin_test_endpoint_available": True,
        }


class OpenWakeWordEngine(WakeWordEngine):
    """Local openWakeWord adapter fed from ALSA raw microphone audio."""

    def __init__(self, cfg: AssistantConfig):
        self.cfg = cfg
        self._pause_event = asyncio.Event()
        self._current_process: asyncio.subprocess.Process | None = None
        self._process_running = False
        self._detection_count = 0
        self._last_detection: dict[str, Any] | None = None

    async def pause(self, reason: str = "") -> None:
        self._pause_event.set()
        proc = self._current_process
        if proc and proc.returncode is None:
            await _terminate_process(proc)

    async def resume(self) -> None:
        self._pause_event.clear()

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
        while not stop_event.is_set():
            if self._pause_event.is_set():
                await _sleep_or_stop(stop_event, 0.1)
                continue
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
            self._current_process = proc
            self._process_running = True
            try:
                while not stop_event.is_set() and not self._pause_event.is_set():
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
                        detection = WakeDetection(
                            phrase=self.cfg.wake.active_wake_phrase or phrase,
                            confidence=float(score),
                            engine="openwakeword",
                            timestamp_monotonic=time.monotonic(),
                        )
                        self._detection_count += 1
                        self._last_detection = detection.__dict__.copy()
                        await callback(detection)
                        try:
                            await asyncio.wait_for(stop_event.wait(), timeout=1.0)
                        except asyncio.TimeoutError:
                            pass
            finally:
                self._process_running = False
                self._current_process = None
                await _terminate_process(proc)

    def status(self) -> dict[str, Any]:
        return {
            "engine": "openwakeword",
            "mode": "production_local_python",
            "production_ready": True,
            "input_source": "local microphone via arecord/openWakeWord",
            "process_running": self._process_running,
            "paused": self._pause_event.is_set(),
            "detection_count": self._detection_count,
            "last_detection": self._last_detection,
            "admin_test_endpoint_available": True,
        }


def _coerce_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_confidence_token(text: str) -> float | None:
    if "confidence=" not in text:
        return None
    try:
        return float(text.split("confidence=", 1)[1].split()[0])
    except ValueError:
        return None


async def _sleep_or_stop(stop_event: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


async def _terminate_process(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=1)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()


def build_wake_engine(cfg: AssistantConfig) -> WakeWordEngine:
    if cfg.wake.engine == "openwakeword":
        return OpenWakeWordEngine(cfg)
    if cfg.wake.engine == "external_command":
        return ExternalCommandWakeWordEngine(
            cfg.wake.external_command,
            cfg.wake.active_wake_phrase,
            sensitivity=cfg.wake.sensitivity,
            capture_device=cfg.audio.capture_device,
            sample_rate_hz=cfg.audio.sample_rate_hz,
            model_path=cfg.wake.model_path,
        )
    return SimulatedWakeWordEngine(cfg.wake.active_wake_phrase)
