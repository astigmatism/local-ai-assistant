from __future__ import annotations

from pathlib import Path

import pytest

from conftest import write_wav
from voice_assistant.config import AssistantConfig
from voice_assistant.tts_voices import (
    KOKORO_VOICES,
    config_with_tts_voice,
    kokoro_voice_options,
    phrase_output_filename,
    regenerate_generated_tts_sounds,
    sanitize_tts_sound_phrase,
    validate_kokoro_voice,
)


class RecordingTTS:
    def __init__(self, *, invalid_wav: bool = False):
        self.invalid_wav = invalid_wav
        self.calls: list[tuple[str, str]] = []

    async def synthesize(self, text, output_path):
        self.calls.append((text, str(output_path)))
        if self.invalid_wav:
            Path(output_path).write_bytes(b"not a wav")
        else:
            write_wav(output_path)
        return Path(output_path)


def test_kokoro_voice_options_are_static_kokoro_only():
    options = kokoro_voice_options()
    ids = [option["id"] for option in options]

    assert ids == list(KOKORO_VOICES)
    assert validate_kokoro_voice("af_heart") == "af_heart"
    assert "bf_emma" in ids
    assert "ff_siwis" in ids
    assert "chatterbox" not in str(options).lower()
    with pytest.raises(ValueError):
        validate_kokoro_voice("cb_fake")


def test_config_with_tts_voice_changes_only_selected_voice():
    cfg = AssistantConfig()
    changed = config_with_tts_voice(cfg, "bf_emma")

    assert changed.services.tts.voice == "bf_emma"
    assert cfg.services.tts.voice == "af_heart"
    assert changed.services.tts.url == cfg.services.tts.url


def test_tts_sound_phrase_sanitization_matches_script_shape():
    assert sanitize_tts_sound_phrase("Wake ack!") == "wake_ack"
    assert sanitize_tts_sound_phrase("I'm ready & listening.") == "im_ready_and_listening"
    assert phrase_output_filename("Prompt accepted") == "prompt_accepted.wav"
    assert phrase_output_filename("!!!") == "sound.wav"


async def test_regenerate_generated_tts_sounds_overwrites_only_phrase_files(tmp_path):
    cfg_data = AssistantConfig().public_dict()
    cfg_data["sounds"]["library_dir"] = str(tmp_path)
    cfg_data["sounds"]["generated_tts_phrases"] = ["wake ack", "failure"]
    cfg = AssistantConfig.model_validate(cfg_data)
    (tmp_path / "wake_ack.wav").write_bytes(b"old wake")
    unrelated = tmp_path / "uploaded.wav"
    unrelated.write_bytes(b"keep me")
    tts = RecordingTTS()

    result = await regenerate_generated_tts_sounds(cfg, lambda _cfg: tts, voice="bf_emma")

    assert result["voice"] == "bf_emma"
    assert {item["filename"] for item in result["generated_files"]} == {"wake_ack.wav", "failure.wav"}
    assert tts.calls[0][0] == "wake ack"
    assert tts.calls[1][0] == "failure"
    assert (tmp_path / "wake_ack.wav").read_bytes() != b"old wake"
    assert unrelated.read_bytes() == b"keep me"


async def test_regenerate_generated_tts_sounds_rejects_invalid_wav_without_overwrite(tmp_path):
    cfg_data = AssistantConfig().public_dict()
    cfg_data["sounds"]["library_dir"] = str(tmp_path)
    cfg_data["sounds"]["generated_tts_phrases"] = ["wake ack"]
    cfg = AssistantConfig.model_validate(cfg_data)
    existing = tmp_path / "wake_ack.wav"
    existing.write_bytes(b"old wake")
    tts = RecordingTTS(invalid_wav=True)

    with pytest.raises(Exception):
        await regenerate_generated_tts_sounds(cfg, lambda _cfg: tts, voice="bf_emma")

    assert existing.read_bytes() == b"old wake"
