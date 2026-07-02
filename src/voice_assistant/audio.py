from __future__ import annotations

import asyncio
import contextlib
import math
import random
import shutil
import signal
import sys
import time
import uuid
import wave
from array import array
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import AssistantConfig, SoundEventFileValue
from .constants import SoundEvent


@dataclass(frozen=True)
class CaptureResult:
    path: str
    duration_seconds: float
    ended_by: str
    rms_peak: float
    bytes_written: int


@dataclass(frozen=True)
class PlaybackPreparationResult:
    source_path: Path
    playback_path: Path
    temporary: bool
    source_sample_rate_hz: int
    source_channels: int
    source_duration_seconds: float
    sample_rate_hz: int
    channels: int
    sample_format: str
    playback_duration_seconds: float
    pre_roll_milliseconds: int
    pre_roll_mode: str
    fade_in_milliseconds: int
    fade_out_milliseconds: int
    silence_tail_milliseconds: int


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


def select_sound_event_file(
    value: SoundEventFileValue,
    choose: Callable[[Sequence[str]], str] | None = None,
) -> str | None:
    """Resolve a configured sound event value to one selected filename for this occurrence."""

    if isinstance(value, str):
        return value or None
    if not value:
        raise ValueError("sound event arrays must not be empty; use an empty string to disable playback")
    selected = (choose or random.choice)(value)
    if not isinstance(selected, str):  # pragma: no cover - defensive against invalid injected choosers
        raise TypeError("sound event random selector must return a string")
    return selected or None


INT16_MIN = -32768
INT16_MAX = 32767


def _clamp_int16(value: float | int) -> int:
    return max(INT16_MIN, min(INT16_MAX, int(round(value))))


def _duration_ms_to_frames(milliseconds: int, sample_rate_hz: int) -> int:
    return max(0, int(round(sample_rate_hz * milliseconds / 1000)))


def _decode_pcm_to_int16(data: bytes, sample_width: int) -> list[int]:
    if sample_width == 1:
        # 8-bit PCM WAV is unsigned; normalize it to signed 16-bit.
        return [int(byte - 128) << 8 for byte in data]
    if sample_width == 2:
        samples = array("h")
        samples.frombytes(data)
        if sys.byteorder != "little":  # pragma: no cover - CI is little-endian, ALSA target is LE.
            samples.byteswap()
        return list(samples)
    if sample_width == 3:
        decoded: list[int] = []
        for index in range(0, len(data), 3):
            chunk = data[index : index + 3]
            if len(chunk) < 3:  # pragma: no cover - wave frames should be complete.
                break
            sign = b"\xff" if chunk[2] & 0x80 else b"\x00"
            decoded.append(int.from_bytes(chunk + sign, "little", signed=True) >> 8)
        return decoded
    if sample_width == 4:
        decoded = []
        for index in range(0, len(data), 4):
            chunk = data[index : index + 4]
            if len(chunk) < 4:  # pragma: no cover - wave frames should be complete.
                break
            decoded.append(int.from_bytes(chunk, "little", signed=True) >> 16)
        return decoded
    raise ValueError(f"unsupported WAV sample width: {sample_width} bytes")


def _encode_int16_le(samples: Sequence[int]) -> bytes:
    encoded = array("h", (_clamp_int16(sample) for sample in samples))
    if sys.byteorder != "little":  # pragma: no cover - CI is little-endian, ALSA target is LE.
        encoded.byteswap()
    return encoded.tobytes()


def _convert_channels(samples: Sequence[int], source_channels: int, target_channels: int) -> list[int]:
    if source_channels <= 0 or target_channels <= 0:
        raise ValueError("channel counts must be positive")
    if source_channels == target_channels:
        return list(samples)

    frame_count = len(samples) // source_channels
    converted: list[int] = []
    for frame_index in range(frame_count):
        start = frame_index * source_channels
        frame = list(samples[start : start + source_channels])
        if target_channels == 1:
            converted.append(_clamp_int16(sum(frame) / len(frame)))
        elif source_channels == 1:
            converted.extend([frame[0]] * target_channels)
        elif target_channels <= source_channels:
            converted.extend(frame[:target_channels])
        else:
            converted.extend(frame)
            converted.extend([frame[-1]] * (target_channels - source_channels))
    return converted


def _resample_interleaved(
    samples: Sequence[int],
    *,
    source_sample_rate_hz: int,
    target_sample_rate_hz: int,
    channels: int,
) -> list[int]:
    if source_sample_rate_hz <= 0 or target_sample_rate_hz <= 0:
        raise ValueError("sample rates must be positive")
    if channels <= 0:
        raise ValueError("channel count must be positive")
    if source_sample_rate_hz == target_sample_rate_hz:
        return list(samples)

    source_frame_count = len(samples) // channels
    if source_frame_count == 0:
        return []
    if source_frame_count == 1:
        target_frame_count = max(1, int(round(target_sample_rate_hz / source_sample_rate_hz)))
        return list(samples[:channels]) * target_frame_count

    target_frame_count = max(1, int(round(source_frame_count * target_sample_rate_hz / source_sample_rate_hz)))
    step = source_sample_rate_hz / target_sample_rate_hz
    resampled: list[int] = []
    for target_frame_index in range(target_frame_count):
        source_position = target_frame_index * step
        left_frame = int(source_position)
        if left_frame >= source_frame_count - 1:
            base = (source_frame_count - 1) * channels
            resampled.extend(samples[base : base + channels])
            continue
        fraction = source_position - left_frame
        left_base = left_frame * channels
        right_base = (left_frame + 1) * channels
        for channel_index in range(channels):
            left = samples[left_base + channel_index]
            right = samples[right_base + channel_index]
            resampled.append(_clamp_int16(left + (right - left) * fraction))
    return resampled


def _preroll_samples(cfg: AssistantConfig, frame_count: int) -> list[int]:
    total_samples = frame_count * cfg.audio.playback_channels
    if total_samples <= 0:
        return []
    if cfg.audio.playback_pre_roll_mode == "comfort_noise" and cfg.audio.playback_comfort_noise_amplitude > 0:
        amplitude = cfg.audio.playback_comfort_noise_amplitude
        noise = random.Random()
        return [noise.randint(-amplitude, amplitude) for _ in range(total_samples)]
    return [0] * total_samples


def _apply_playback_envelope_and_padding(
    cfg: AssistantConfig,
    speech_samples: Sequence[int],
    *,
    apply_start_stop_mitigation: bool,
) -> list[int]:
    channels = cfg.audio.playback_channels
    sample_rate = cfg.audio.playback_sample_rate_hz
    speech_frame_count = len(speech_samples) // channels
    if not apply_start_stop_mitigation:
        return list(speech_samples)

    pre_roll_frames = _duration_ms_to_frames(cfg.audio.playback_pre_roll_milliseconds, sample_rate)
    tail_frames = _duration_ms_to_frames(cfg.audio.playback_silence_tail_milliseconds, sample_rate)
    fade_in_frames = min(_duration_ms_to_frames(cfg.audio.playback_fade_in_milliseconds, sample_rate), speech_frame_count)
    fade_out_frames = min(_duration_ms_to_frames(cfg.audio.playback_fade_out_milliseconds, sample_rate), speech_frame_count)

    mitigated = _preroll_samples(cfg, pre_roll_frames)
    for frame_index in range(speech_frame_count):
        scale = 1.0
        if fade_in_frames > 0 and frame_index < fade_in_frames:
            scale = min(scale, frame_index / fade_in_frames)
        if fade_out_frames > 0 and frame_index >= speech_frame_count - fade_out_frames:
            remaining = speech_frame_count - frame_index - 1
            scale = min(scale, remaining / fade_out_frames)
        source_offset = frame_index * channels
        for channel_index in range(channels):
            sample = speech_samples[source_offset + channel_index]
            mitigated.append(_clamp_int16(sample * scale))
    mitigated.extend([0] * tail_frames * channels)
    return mitigated


def prepare_wav_for_playback(
    cfg: AssistantConfig,
    source_path: str | Path,
    output_path: str | Path,
    *,
    temporary: bool = False,
    apply_start_stop_mitigation: bool = True,
) -> PlaybackPreparationResult:
    """Normalize a WAV file for ALSA speaker playback and add start/stop transient mitigation."""

    source = Path(source_path)
    output = Path(output_path)
    with wave.open(str(source), "rb") as wav:
        source_channels = wav.getnchannels()
        source_sample_width = wav.getsampwidth()
        source_sample_rate = wav.getframerate()
        source_frame_count = wav.getnframes()
        frames = wav.readframes(source_frame_count)

    decoded = _decode_pcm_to_int16(frames, source_sample_width)
    converted = _convert_channels(decoded, source_channels, cfg.audio.playback_channels)
    resampled = _resample_interleaved(
        converted,
        source_sample_rate_hz=source_sample_rate,
        target_sample_rate_hz=cfg.audio.playback_sample_rate_hz,
        channels=cfg.audio.playback_channels,
    )
    mitigated = _apply_playback_envelope_and_padding(
        cfg,
        resampled,
        apply_start_stop_mitigation=apply_start_stop_mitigation,
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output), "wb") as wav:
        wav.setnchannels(cfg.audio.playback_channels)
        wav.setsampwidth(2)
        wav.setframerate(cfg.audio.playback_sample_rate_hz)
        wav.writeframes(_encode_int16_le(mitigated))

    playback_frame_count = len(mitigated) // cfg.audio.playback_channels
    return PlaybackPreparationResult(
        source_path=source,
        playback_path=output,
        temporary=temporary,
        source_sample_rate_hz=source_sample_rate,
        source_channels=source_channels,
        source_duration_seconds=source_frame_count / source_sample_rate if source_sample_rate else 0.0,
        sample_rate_hz=cfg.audio.playback_sample_rate_hz,
        channels=cfg.audio.playback_channels,
        sample_format=cfg.audio.playback_sample_format,
        playback_duration_seconds=playback_frame_count / cfg.audio.playback_sample_rate_hz,
        pre_roll_milliseconds=cfg.audio.playback_pre_roll_milliseconds if apply_start_stop_mitigation else 0,
        pre_roll_mode=cfg.audio.playback_pre_roll_mode if apply_start_stop_mitigation else "disabled",
        fade_in_milliseconds=cfg.audio.playback_fade_in_milliseconds if apply_start_stop_mitigation else 0,
        fade_out_milliseconds=cfg.audio.playback_fade_out_milliseconds if apply_start_stop_mitigation else 0,
        silence_tail_milliseconds=cfg.audio.playback_silence_tail_milliseconds if apply_start_stop_mitigation else 0,
    )


class AudioController:
    """Thin wrapper around ALSA utilities used by the Ubuntu speakerphone client."""

    def __init__(self, *, sound_choice: Callable[[Sequence[str]], str] | None = None):
        self._effect_lock = asyncio.Lock()
        self._process_lock = asyncio.Lock()
        self._processes: set[asyncio.subprocess.Process] = set()
        self._sound_choice = sound_choice or random.choice

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

    def resolve_sound_filename(self, cfg: AssistantConfig, event: SoundEvent | str) -> str | None:
        event_key = SoundEvent(event)
        return select_sound_event_file(cfg.sounds.event_files[event_key], self._sound_choice)

    def resolve_sound_path(self, cfg: AssistantConfig, event: SoundEvent | str) -> Path | None:
        filename = self.resolve_sound_filename(cfg, event)
        if filename is None:
            return None
        path = Path(filename)
        if not path.is_absolute():
            path = Path(cfg.sounds.library_dir) / path
        return path

    async def _play_resolved_sound_path(
        self,
        cfg: AssistantConfig,
        path: Path,
        *,
        cancel_event: asyncio.Event | None,
        serialize: bool,
        require_playback: bool,
        apply_start_stop_mitigation: bool,
    ) -> None:
        if serialize:
            async with self._effect_lock:
                if require_playback:
                    await self.play_file(
                        cfg,
                        path,
                        cancel_event=cancel_event,
                        require_playback=True,
                        apply_start_stop_mitigation=apply_start_stop_mitigation,
                    )
                else:
                    await self.play_file(
                        cfg,
                        path,
                        cancel_event=cancel_event,
                        apply_start_stop_mitigation=apply_start_stop_mitigation,
                    )
        elif require_playback:
            await self.play_file(
                cfg,
                path,
                cancel_event=cancel_event,
                require_playback=True,
                apply_start_stop_mitigation=apply_start_stop_mitigation,
            )
        else:
            await self.play_file(
                cfg,
                path,
                cancel_event=cancel_event,
                apply_start_stop_mitigation=apply_start_stop_mitigation,
            )

    async def play_sound_event(
        self,
        cfg: AssistantConfig,
        event: SoundEvent | str,
        *,
        cancel_event: asyncio.Event | None = None,
        serialize: bool = True,
        require_playback: bool = False,
        apply_start_stop_mitigation: bool | None = None,
    ) -> None:
        path = self.resolve_sound_path(cfg, event)
        if path is None:
            return
        await self._play_resolved_sound_path(
            cfg,
            path,
            cancel_event=cancel_event,
            serialize=serialize,
            require_playback=require_playback,
            apply_start_stop_mitigation=(
                cfg.audio.playback_sound_event_start_stop_mitigation_enabled
                if apply_start_stop_mitigation is None
                else apply_start_stop_mitigation
            ),
        )

    def new_playback_path(self, cfg: AssistantConfig, source_path: str | Path | None = None) -> Path:
        source_stem = Path(source_path).stem if source_path else "audio"
        return Path(cfg.storage.artifacts_dir) / "scratch" / f"playback-{source_stem}-{uuid.uuid4()}.wav"

    def prepare_playback_file(
        self,
        cfg: AssistantConfig,
        path: str | Path,
        *,
        output_path: str | Path | None = None,
        temporary: bool = False,
        apply_start_stop_mitigation: bool = True,
    ) -> PlaybackPreparationResult:
        source = Path(path)
        if not cfg.audio.playback_preparation_enabled:
            with wave.open(str(source), "rb") as wav:
                source_frame_count = wav.getnframes()
                source_rate = wav.getframerate()
                source_channels = wav.getnchannels()
            return PlaybackPreparationResult(
                source_path=source,
                playback_path=source,
                temporary=False,
                source_sample_rate_hz=source_rate,
                source_channels=source_channels,
                source_duration_seconds=source_frame_count / source_rate if source_rate else 0.0,
                sample_rate_hz=source_rate,
                channels=source_channels,
                sample_format="source_wav",
                playback_duration_seconds=source_frame_count / source_rate if source_rate else 0.0,
                pre_roll_milliseconds=0,
                pre_roll_mode="disabled",
                fade_in_milliseconds=0,
                fade_out_milliseconds=0,
                silence_tail_milliseconds=0,
            )
        target = Path(output_path) if output_path is not None else self.new_playback_path(cfg, source)
        return prepare_wav_for_playback(
            cfg,
            source,
            target,
            temporary=temporary,
            apply_start_stop_mitigation=apply_start_stop_mitigation,
        )

    def _aplay_command(self, cfg: AssistantConfig, path: Path) -> list[str]:
        command = ["aplay", "-q", "-D", cfg.audio.playback_device]
        if cfg.audio.playback_buffer_time_microseconds is not None:
            command.extend(["-B", str(cfg.audio.playback_buffer_time_microseconds)])
        if cfg.audio.playback_period_time_microseconds is not None:
            command.extend(["-F", str(cfg.audio.playback_period_time_microseconds)])
        command.append(str(path))
        return command

    async def play_file(
        self,
        cfg: AssistantConfig,
        path: str | Path,
        *,
        cancel_event: asyncio.Event | None = None,
        require_playback: bool = False,
        apply_start_stop_mitigation: bool = True,
    ) -> None:
        path = Path(path)
        if not path.exists():
            if require_playback:
                raise FileNotFoundError(path)
            return
        if shutil.which("aplay") is None:
            if require_playback:
                raise RuntimeError("aplay is required for audio playback")
            return
        prepared: PlaybackPreparationResult | None = None
        playback_path = path
        delete_playback_path = False
        if cfg.audio.playback_preparation_enabled:
            prepared = self.prepare_playback_file(
                cfg,
                path,
                temporary=True,
                apply_start_stop_mitigation=apply_start_stop_mitigation,
            )
            playback_path = prepared.playback_path
            delete_playback_path = prepared.temporary
        await self.ensure_output_volume(cfg)
        proc = await asyncio.create_subprocess_exec(
            *self._aplay_command(cfg, playback_path),
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
            if delete_playback_path:
                with contextlib.suppress(OSError):
                    playback_path.unlink()

    def start_looping_sound(self, cfg: AssistantConfig, event: SoundEvent | str) -> LoopingSoundHandle:
        stop_event = asyncio.Event()
        path = self.resolve_sound_path(cfg, event)
        if path is None:
            task = asyncio.create_task(self._no_sound_loop())
        else:
            task = asyncio.create_task(self._loop_resolved_sound_path(cfg, path, stop_event))
        return LoopingSoundHandle(stop_event=stop_event, task=task)

    async def _no_sound_loop(self) -> None:
        return

    async def _loop_sound(self, cfg: AssistantConfig, event: SoundEvent | str, stop_event: asyncio.Event) -> None:
        path = self.resolve_sound_path(cfg, event)
        if path is None:
            return
        await self._loop_resolved_sound_path(cfg, path, stop_event)

    async def _loop_resolved_sound_path(self, cfg: AssistantConfig, path: Path, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await self._play_resolved_sound_path(
                    cfg,
                    path,
                    cancel_event=stop_event,
                    serialize=True,
                    require_playback=False,
                    apply_start_stop_mitigation=cfg.audio.playback_sound_event_start_stop_mitigation_enabled,
                )
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
