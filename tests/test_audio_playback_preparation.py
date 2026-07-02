from __future__ import annotations

import wave
from array import array
from pathlib import Path

from voice_assistant.audio import AudioController
from voice_assistant.config import AssistantConfig


def _write_constant_wav(
    path: Path,
    *,
    duration_seconds: float = 0.5,
    sample_rate_hz: int = 16000,
    channels: int = 1,
    sample: int = 10000,
) -> Path:
    frame_count = int(round(duration_seconds * sample_rate_hz))
    samples = array("h", [sample] * frame_count * channels)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate_hz)
        wav.writeframes(samples.tobytes())
    return path


def _read_int16_wav(path: Path) -> tuple[int, int, int, list[int]]:
    with wave.open(str(path), "rb") as wav:
        channels = wav.getnchannels()
        sample_width = wav.getsampwidth()
        sample_rate = wav.getframerate()
        frames = wav.readframes(wav.getnframes())
    samples = array("h")
    samples.frombytes(frames)
    return channels, sample_width, sample_rate, list(samples)


def _frames(milliseconds: int, sample_rate_hz: int) -> int:
    return int(round(sample_rate_hz * milliseconds / 1000))


def test_prepare_playback_file_normalizes_wav_and_adds_start_stop_mitigation(tmp_path):
    cfg = AssistantConfig()
    source = _write_constant_wav(tmp_path / "source-16k-mono.wav")
    output = tmp_path / "prepared.wav"

    result = AudioController().prepare_playback_file(cfg, source, output_path=output)

    assert result.source_sample_rate_hz == 16000
    assert result.source_channels == 1
    assert result.sample_rate_hz == 48000
    assert result.channels == 2
    assert result.sample_format == "S16_LE"
    assert result.pre_roll_milliseconds == 350
    assert result.fade_in_milliseconds == 100
    assert result.fade_out_milliseconds == 150
    assert result.silence_tail_milliseconds == 500

    channels, sample_width, sample_rate, samples = _read_int16_wav(output)
    assert channels == 2
    assert sample_width == 2
    assert sample_rate == 48000

    speech_frames = int(round(0.5 * sample_rate))
    pre_roll_frames = _frames(cfg.audio.playback_pre_roll_milliseconds, sample_rate)
    tail_frames = _frames(cfg.audio.playback_silence_tail_milliseconds, sample_rate)
    fade_in_frames = _frames(cfg.audio.playback_fade_in_milliseconds, sample_rate)
    fade_out_frames = _frames(cfg.audio.playback_fade_out_milliseconds, sample_rate)
    total_frames = len(samples) // channels

    assert total_frames == pre_roll_frames + speech_frames + tail_frames
    assert samples[: pre_roll_frames * channels] == [0] * pre_roll_frames * channels
    assert samples[pre_roll_frames * channels : (pre_roll_frames + 1) * channels] == [0, 0]

    after_fade_offset = (pre_roll_frames + fade_in_frames + 20) * channels
    assert samples[after_fade_offset] == samples[after_fade_offset + 1]
    assert 9800 <= samples[after_fade_offset] <= 10000

    fade_out_start = pre_roll_frames + speech_frames - fade_out_frames
    before_fade_out = abs(samples[(fade_out_start - 20) * channels])
    middle_fade_out = abs(samples[(fade_out_start + fade_out_frames // 2) * channels])
    assert before_fade_out >= 9800
    assert 3500 <= middle_fade_out <= 6500
    assert middle_fade_out < before_fade_out

    tail_start = (pre_roll_frames + speech_frames) * channels
    assert samples[tail_start:] == [0] * tail_frames * channels



def test_playback_preparation_can_normalize_without_start_stop_padding(tmp_path):
    cfg = AssistantConfig()
    source = _write_constant_wav(tmp_path / "sound-event-source.wav", duration_seconds=0.2)
    output = tmp_path / "sound-event-prepared.wav"

    result = AudioController().prepare_playback_file(
        cfg,
        source,
        output_path=output,
        apply_start_stop_mitigation=False,
    )

    channels, sample_width, sample_rate, samples = _read_int16_wav(output)
    assert channels == 2
    assert sample_width == 2
    assert sample_rate == 48000
    assert len(samples) // channels == int(round(0.2 * sample_rate))
    assert result.pre_roll_milliseconds == 0
    assert result.pre_roll_mode == "disabled"
    assert result.fade_in_milliseconds == 0
    assert result.fade_out_milliseconds == 0
    assert result.silence_tail_milliseconds == 0
    assert samples[:2] == [10000, 10000]


def test_playback_preparation_can_be_disabled_without_changing_capture_defaults(tmp_path):
    data = AssistantConfig().public_dict()
    data["audio"]["playback_preparation_enabled"] = False
    cfg = AssistantConfig.model_validate(data)
    source = _write_constant_wav(tmp_path / "source.wav")

    result = AudioController().prepare_playback_file(cfg, source)

    assert result.playback_path == source
    assert result.sample_rate_hz == 16000
    assert result.channels == 1
    assert result.pre_roll_milliseconds == 0
    assert cfg.audio.sample_rate_hz == 16000
    assert cfg.audio.channels == 1
    assert cfg.audio.playback_sample_rate_hz == 48000
    assert cfg.audio.playback_channels == 2


def test_aplay_command_uses_configured_device_and_stable_buffer_settings():
    cfg = AssistantConfig()
    command = AudioController()._aplay_command(cfg, Path("prepared.wav"))

    assert command[:4] == ["aplay", "-q", "-D", "plughw:0,0"]
    assert command[-1] == "prepared.wav"
    assert command[command.index("-B") + 1] == "1000000"
    assert command[command.index("-F") + 1] == "250000"
