from __future__ import annotations

import asyncio
import math
import shutil
import signal
import time
import uuid
import wave
from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import AssistantConfig
from .constants import SoundEvent


@dataclass(frozen=True)
class CaptureResult:
    path: str
    duration_seconds: float
    ended_by: str
    rms_peak: float
    bytes_written: int


@dataclass
class LoopingSoundHandle:
    stop_event: asyncio.Event
    task: asyncio.Task[Any]

    async def stop(self) -> None:
        self.stop_event.set()
        if not self.task.done():
            try:
                await asyncio.wait_for(self.task, timeout=3)
            except asyncio.TimeoutError:
                self.task.cancel()
            except asyncio.CancelledError:
                pass


def rms_int16_le(data: bytes) -> float:
    if not data:
        return 0.0
    samples = array("h")
    samples.frombytes(data)
    if not samples:
        return 0.0
    # ALSA S16_LE is little endian. array uses native endian; byteswap on big-endian hosts.
    if samples.itemsize != 2:  # pragma: no cover - defensive
        return 0.0
    total = 0
    for sample in samples:
        total += sample * sample
    return math.sqrt(total / len(samples))


class AudioController:
    """Thin wrapper around ALSA utilities used by the Ubuntu speakerphone client."""

    def __init__(self):
        self._effect_lock = asyncio.Lock()
        self._process_lock = asyncio.Lock()
        self._processes: set[asyncio.subprocess.Process] = set()

    async def ensure_output_volume(self, cfg: AssistantConfig) -> None:
        if cfg.audio.enforce_pcm_volume_percent is None:
            return
        if shutil.which("amixer") is None:
            return
        proc = await asyncio.create_subprocess_exec(
            "amixer",
            "-c",
            str(cfg.audio.mixer_card_index),
            "sset",
            "PCM",
            f"{cfg.audio.enforce_pcm_volume_percent}%",
            "unmute",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()

    def resolve_sound_path(self, cfg: AssistantConfig, event: SoundEvent | str) -> Path | None:
        event_key = SoundEvent(event)
        filename = cfg.sounds.event_files[event_key]
        if filename == "":
            return None
        path = Path(filename)
        if not path.is_absolute():
            path = Path(cfg.sounds.library_dir) / path
        return path

    async def play_sound_event(
        self,
        cfg: AssistantConfig,
        event: SoundEvent | str,
        *,
        cancel_event: asyncio.Event | None = None,
        serialize: bool = True,
    ) -> None:
        path = self.resolve_sound_path(cfg, event)
        if path is None:
            return
        if serialize:
            async with self._effect_lock:
                await self.play_file(cfg, path, cancel_event=cancel_event)
        else:
            await self.play_file(cfg, path, cancel_event=cancel_event)

    async def play_file(
        self,
        cfg: AssistantConfig,
        path: str | Path,
        *,
        cancel_event: asyncio.Event | None = None,
    ) -> None:
        path = Path(path)
        if not path.exists():
            return
        if shutil.which("aplay") is None:
            return
        await self.ensure_output_volume(cfg)
        proc = await asyncio.create_subprocess_exec(
            "aplay",
            "-q",
            "-D",
            cfg.audio.playback_device,
            str(path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        async with self._process_lock:
            self._processes.add(proc)
        try:
            while True:
                if cancel_event and cancel_event.is_set():
                    await self._terminate(proc)
                    raise asyncio.CancelledError
                try:
                    await asyncio.wait_for(proc.wait(), timeout=0.1)
                    break
                except asyncio.TimeoutError:
                    continue
            if proc.returncode not in (0, None):
                stderr = b""
                if proc.stderr:
                    stderr = await proc.stderr.read()
                raise RuntimeError(f"aplay failed: {stderr.decode(errors='replace')}")
        finally:
            async with self._process_lock:
                self._processes.discard(proc)

    def start_looping_sound(self, cfg: AssistantConfig, event: SoundEvent | str) -> LoopingSoundHandle:
        stop_event = asyncio.Event()
        if self.resolve_sound_path(cfg, event) is None:
            task = asyncio.create_task(self._no_sound_loop())
        else:
            task = asyncio.create_task(self._loop_sound(cfg, event, stop_event))
        return LoopingSoundHandle(stop_event=stop_event, task=task)

    async def _no_sound_loop(self) -> None:
        return

    async def _loop_sound(self, cfg: AssistantConfig, event: SoundEvent | str, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await self.play_sound_event(cfg, event, cancel_event=stop_event, serialize=True)
            except asyncio.CancelledError:
                break
            # Avoid a busy loop when audio utilities are not installed or file is missing.
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=0.05)
            except asyncio.TimeoutError:
                pass

    async def stop_all_playback(self) -> None:
        async with self._process_lock:
            processes = list(self._processes)
        for proc in processes:
            await self._terminate(proc)

    async def _terminate(self, proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None:
            return
        try:
            proc.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=1)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                return
            await proc.wait()

    async def record_prompt(
        self,
        cfg: AssistantConfig,
        output_path: str | Path,
        cancel_event: asyncio.Event | None = None,
    ) -> CaptureResult:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if shutil.which("arecord") is None:
            raise RuntimeError("arecord is required for microphone capture")

        bytes_per_sample = 2
        chunk_frames = int(cfg.audio.sample_rate_hz * cfg.prompt_capture.chunk_milliseconds / 1000)
        chunk_bytes = chunk_frames * cfg.audio.channels * bytes_per_sample
        start = time.monotonic()
        silence_seconds = 0.0
        bytes_written = 0
        rms_peak = 0.0
        ended_by = "maximum_duration"

        proc = await asyncio.create_subprocess_exec(
            "arecord",
            "-q",
            "-D",
            cfg.audio.capture_device,
            "-f",
            "S16_LE",
            "-r",
            str(cfg.audio.sample_rate_hz),
            "-c",
            str(cfg.audio.channels),
            "-t",
            "raw",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        async with self._process_lock:
            self._processes.add(proc)
        try:
            with wave.open(str(path), "wb") as wav:
                wav.setnchannels(cfg.audio.channels)
                wav.setsampwidth(bytes_per_sample)
                wav.setframerate(cfg.audio.sample_rate_hz)
                while True:
                    if cancel_event and cancel_event.is_set():
                        ended_by = "cancelled"
                        await self._terminate(proc)
                        raise asyncio.CancelledError
                    elapsed = time.monotonic() - start
                    if elapsed >= cfg.prompt_capture.maximum_duration_seconds:
                        ended_by = "maximum_duration"
                        break
                    try:
                        chunk = await asyncio.wait_for(proc.stdout.readexactly(chunk_bytes), timeout=1.0)  # type: ignore[union-attr]
                    except asyncio.IncompleteReadError as exc:
                        chunk = exc.partial
                        if not chunk:
                            ended_by = "capture_stream_ended"
                            break
                    except asyncio.TimeoutError:
                        continue
                    wav.writeframes(chunk)
                    bytes_written += len(chunk)
                    rms = rms_int16_le(chunk)
                    rms_peak = max(rms_peak, rms)
                    elapsed = time.monotonic() - start
                    if elapsed >= cfg.prompt_capture.minimum_duration_seconds:
                        if rms <= cfg.prompt_capture.silence_rms_threshold:
                            silence_seconds += cfg.prompt_capture.chunk_milliseconds / 1000
                        else:
                            silence_seconds = 0.0
                        if silence_seconds >= cfg.prompt_capture.silence_duration_seconds:
                            ended_by = "silence"
                            break
            await self._terminate(proc)
            return CaptureResult(
                path=str(path),
                duration_seconds=time.monotonic() - start,
                ended_by=ended_by,
                rms_peak=rms_peak,
                bytes_written=bytes_written,
            )
        finally:
            await self._terminate(proc)
            async with self._process_lock:
                self._processes.discard(proc)

    async def record_fixed_duration(
        self,
        cfg: AssistantConfig,
        duration_seconds: float,
        output_path: str | Path,
    ) -> CaptureResult:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if shutil.which("arecord") is None:
            raise RuntimeError("arecord is required for microphone capture")
        start = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            "arecord",
            "-q",
            "-D",
            cfg.audio.capture_device,
            "-f",
            "S16_LE",
            "-r",
            str(cfg.audio.sample_rate_hz),
            "-c",
            str(cfg.audio.channels),
            "-d",
            str(max(1, int(duration_seconds))),
            str(path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        async with self._process_lock:
            self._processes.add(proc)
        try:
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(f"arecord failed: {stderr.decode(errors='replace')}")
            return CaptureResult(
                path=str(path),
                duration_seconds=time.monotonic() - start,
                ended_by="duration",
                rms_peak=0.0,
                bytes_written=path.stat().st_size if path.exists() else 0,
            )
        finally:
            async with self._process_lock:
                self._processes.discard(proc)

    def new_prompt_path(self, cfg: AssistantConfig, interaction_id: str | None = None) -> Path:
        interaction_id = interaction_id or str(uuid.uuid4())
        return Path(cfg.storage.artifacts_dir) / "scratch" / f"prompt-{interaction_id}.wav"

    def new_tts_path(self, cfg: AssistantConfig, interaction_id: str | None = None) -> Path:
        interaction_id = interaction_id or str(uuid.uuid4())
        return Path(cfg.storage.artifacts_dir) / "scratch" / f"tts-{interaction_id}.wav"
