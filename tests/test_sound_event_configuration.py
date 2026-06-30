from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError

from voice_assistant.audio import AudioController
from voice_assistant.config import AssistantConfig, ConfigStore
from voice_assistant.constants import SoundEvent


def _config_with_event_file(tmp_path: Path, event: SoundEvent, filename: str) -> AssistantConfig:
    data = AssistantConfig().public_dict()
    data["sounds"]["library_dir"] = str(tmp_path / "sounds")
    data["sounds"]["event_files"][event.value] = filename
    return AssistantConfig.model_validate(data)


def test_sound_event_configuration_accepts_empty_string_and_rejects_missing_event():
    data = AssistantConfig().public_dict()
    data["sounds"]["event_files"][SoundEvent.PROMPT_ACCEPTED.value] = ""

    cfg = AssistantConfig.model_validate(data)

    assert cfg.sounds.event_files[SoundEvent.PROMPT_ACCEPTED] == ""

    missing = AssistantConfig().public_dict()
    missing["sounds"]["event_files"].pop(SoundEvent.PROMPT_ACCEPTED.value)
    with pytest.raises(ValidationError, match="missing sound event mappings"):
        AssistantConfig.model_validate(missing)


async def test_empty_sound_event_is_safe_noop_without_play_file(monkeypatch, tmp_path):
    cfg = _config_with_event_file(tmp_path, SoundEvent.PROMPT_ACCEPTED, "")
    audio = AudioController()

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("empty sound events must not attempt file playback")

    monkeypatch.setattr(audio, "play_file", fail_if_called)

    assert audio.resolve_sound_path(cfg, SoundEvent.PROMPT_ACCEPTED) is None
    await audio.play_sound_event(cfg, SoundEvent.PROMPT_ACCEPTED)


async def test_non_empty_sound_event_uses_existing_resolved_playback_path(monkeypatch, tmp_path):
    cfg = _config_with_event_file(tmp_path, SoundEvent.PROMPT_ACCEPTED, "prompt_accepted.wav")
    audio = AudioController()
    played_paths: list[Path] = []

    async def record_play_file(cfg_arg, path, *, cancel_event=None):
        played_paths.append(Path(path))

    monkeypatch.setattr(audio, "play_file", record_play_file)

    await audio.play_sound_event(cfg, SoundEvent.PROMPT_ACCEPTED)

    assert played_paths == [Path(cfg.sounds.library_dir) / "prompt_accepted.wav"]


def test_whitespace_only_sound_event_value_is_still_a_configured_reference(tmp_path):
    cfg = _config_with_event_file(tmp_path, SoundEvent.PROMPT_ACCEPTED, " ")
    audio = AudioController()

    assert audio.resolve_sound_path(cfg, SoundEvent.PROMPT_ACCEPTED) == Path(cfg.sounds.library_dir) / " "


async def test_empty_looping_sound_event_completes_without_playback(monkeypatch, tmp_path):
    cfg = _config_with_event_file(tmp_path, SoundEvent.THINKING, "")
    audio = AudioController()

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("empty looping sound events must not attempt file playback")

    monkeypatch.setattr(audio, "play_file", fail_if_called)

    handle = audio.start_looping_sound(cfg, SoundEvent.THINKING)
    await asyncio.sleep(0)

    assert handle.task.done()
    await handle.stop()


def test_config_persistence_preserves_empty_sound_event_file(tmp_path):
    store = ConfigStore(tmp_path / "config.json")
    data = store.get_saved().public_dict()
    data["sounds"]["event_files"][SoundEvent.PROMPT_ACCEPTED.value] = ""

    result = store.apply_config(data)

    assert result.saved["sounds"]["event_files"][SoundEvent.PROMPT_ACCEPTED.value] == ""
    reloaded = ConfigStore(tmp_path / "config.json")
    assert reloaded.get_saved().sounds.event_files[SoundEvent.PROMPT_ACCEPTED] == ""
