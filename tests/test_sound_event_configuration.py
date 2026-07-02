from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from voice_assistant.audio import AudioController, select_sound_event_file
from voice_assistant.config import AssistantConfig, ConfigStore
from voice_assistant.constants import SoundEvent


def _config_with_event_file(tmp_path: Path, event: SoundEvent, value: str | list[str]) -> AssistantConfig:
    data = AssistantConfig().public_dict()
    data["sounds"]["library_dir"] = str(tmp_path / "sounds")
    data["sounds"]["event_files"][event.value] = value
    return AssistantConfig.model_validate(data)


def test_sound_event_configuration_accepts_empty_string_array_values_and_rejects_missing_event():
    data = AssistantConfig().public_dict()
    data["sounds"]["event_files"][SoundEvent.PROMPT_ACCEPTED.value] = ""
    data["sounds"]["event_files"][SoundEvent.INVALID_PROMPT.value] = ["invalid_1.wav", "invalid_2.wav"]

    cfg = AssistantConfig.model_validate(data)

    assert cfg.sounds.event_files[SoundEvent.PROMPT_ACCEPTED] == ""
    assert cfg.sounds.event_files[SoundEvent.INVALID_PROMPT] == ["invalid_1.wav", "invalid_2.wav"]

    missing = AssistantConfig().public_dict()
    missing["sounds"]["event_files"].pop(SoundEvent.PROMPT_ACCEPTED.value)
    with pytest.raises(ValidationError, match="missing sound event mappings"):
        AssistantConfig.model_validate(missing)


def test_default_config_includes_new_conversation_wake_ack_event():
    cfg = AssistantConfig()

    assert SoundEvent.WAKE_NEW_CONVERSATION in cfg.sounds.event_files
    assert cfg.sounds.event_files[SoundEvent.WAKE_NEW_CONVERSATION] == cfg.sounds.event_files[SoundEvent.WAKE_ACK]


def test_legacy_config_missing_command_thinking_is_backfilled_from_thinking(tmp_path):
    data = AssistantConfig().public_dict()
    data["sounds"]["event_files"][SoundEvent.THINKING.value] = "current-thinking.wav"
    data["sounds"]["event_files"].pop(SoundEvent.COMMAND_THINKING.value)

    cfg = AssistantConfig.model_validate(data)

    assert cfg.sounds.event_files[SoundEvent.COMMAND_THINKING] == "current-thinking.wav"


def test_legacy_config_missing_wake_new_conversation_is_backfilled_from_wake_ack(tmp_path):
    data = AssistantConfig().public_dict()
    data["sounds"]["event_files"][SoundEvent.WAKE_ACK.value] = "current-wake.wav"
    data["sounds"]["event_files"].pop(SoundEvent.WAKE_NEW_CONVERSATION.value)

    cfg = AssistantConfig.model_validate(data)

    assert cfg.sounds.event_files[SoundEvent.WAKE_NEW_CONVERSATION] == "current-wake.wav"


def test_legacy_config_missing_wake_new_conversation_preserves_array_wake_ack(tmp_path):
    data = AssistantConfig().public_dict()
    data["sounds"]["event_files"][SoundEvent.WAKE_ACK.value] = ["wake-a.wav", "wake-b.wav"]
    data["sounds"]["event_files"].pop(SoundEvent.WAKE_NEW_CONVERSATION.value)

    cfg = AssistantConfig.model_validate(data)

    assert cfg.sounds.event_files[SoundEvent.WAKE_NEW_CONVERSATION] == ["wake-a.wav", "wake-b.wav"]
    assert cfg.sounds.event_files[SoundEvent.WAKE_NEW_CONVERSATION] is not cfg.sounds.event_files[SoundEvent.WAKE_ACK]


def test_config_store_persists_backfilled_command_thinking_and_wake_new_conversation_for_existing_files(tmp_path):
    config_path = tmp_path / "config.json"
    data = AssistantConfig().public_dict()
    data["sounds"]["event_files"][SoundEvent.THINKING.value] = "legacy-thinking.wav"
    data["sounds"]["event_files"][SoundEvent.WAKE_ACK.value] = "legacy-wake.wav"
    data["sounds"]["event_files"].pop(SoundEvent.COMMAND_THINKING.value)
    data["sounds"]["event_files"].pop(SoundEvent.WAKE_NEW_CONVERSATION.value)
    ConfigStore._write_json(config_path, data)

    store = ConfigStore(config_path)

    assert store.get_saved().sounds.event_files[SoundEvent.COMMAND_THINKING] == "legacy-thinking.wav"
    assert store.get_saved().sounds.event_files[SoundEvent.WAKE_NEW_CONVERSATION] == "legacy-wake.wav"
    reloaded = ConfigStore(config_path)
    assert reloaded.get_saved().sounds.event_files[SoundEvent.COMMAND_THINKING] == "legacy-thinking.wav"
    assert reloaded.get_saved().sounds.event_files[SoundEvent.WAKE_NEW_CONVERSATION] == "legacy-wake.wav"


@pytest.mark.parametrize(
    "bad_value",
    [[], ["valid.wav", 123], [object()], None],
)
def test_sound_event_configuration_rejects_malformed_event_values(bad_value: Any):
    data = AssistantConfig().public_dict()
    data["sounds"]["event_files"][SoundEvent.PROMPT_ACCEPTED.value] = bad_value

    with pytest.raises(ValidationError):
        AssistantConfig.model_validate(data)


def test_runtime_sound_resolution_returns_single_string_and_empty_string():
    assert select_sound_event_file("prompt_accepted.wav") == "prompt_accepted.wav"
    assert select_sound_event_file("") is None


def test_runtime_sound_resolution_uses_injected_choice_for_arrays():
    choices_seen: list[list[str]] = []

    def choose(options):
        choices_seen.append(list(options))
        return options[1]

    selected = select_sound_event_file(["first.wav", "second.wav", "third.wav"], choose)

    assert selected == "second.wav"
    assert choices_seen == [["first.wav", "second.wav", "third.wav"]]


def test_runtime_sound_resolution_treats_selected_empty_array_entry_as_no_sound():
    assert select_sound_event_file(["sound.wav", ""], lambda options: options[1]) is None


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
    playback_kwargs: list[dict[str, object]] = []

    async def record_play_file(cfg_arg, path, *, cancel_event=None, **kwargs):
        played_paths.append(Path(path))
        playback_kwargs.append(kwargs)

    monkeypatch.setattr(audio, "play_file", record_play_file)

    await audio.play_sound_event(cfg, SoundEvent.PROMPT_ACCEPTED)

    assert played_paths == [Path(cfg.sounds.library_dir) / "prompt_accepted.wav"]
    assert playback_kwargs == [{"apply_start_stop_mitigation": False}]


async def test_array_sound_event_selects_at_each_playback_request(monkeypatch, tmp_path):
    cfg = _config_with_event_file(tmp_path, SoundEvent.INVALID_PROMPT, ["invalid-a.wav", "invalid-b.wav"])
    selected_indices = iter([0, 1, 0])
    audio = AudioController(sound_choice=lambda options: options[next(selected_indices)])
    played_paths: list[Path] = []

    async def record_play_file(cfg_arg, path, *, cancel_event=None, **kwargs):
        played_paths.append(Path(path))

    monkeypatch.setattr(audio, "play_file", record_play_file)

    await audio.play_sound_event(cfg, SoundEvent.INVALID_PROMPT)
    await audio.play_sound_event(cfg, SoundEvent.INVALID_PROMPT)
    await audio.play_sound_event(cfg, SoundEvent.INVALID_PROMPT)

    assert played_paths == [
        Path(cfg.sounds.library_dir) / "invalid-a.wav",
        Path(cfg.sounds.library_dir) / "invalid-b.wav",
        Path(cfg.sounds.library_dir) / "invalid-a.wav",
    ]


async def test_array_sound_event_selected_empty_entry_does_not_play(monkeypatch, tmp_path):
    cfg = _config_with_event_file(tmp_path, SoundEvent.INVALID_PROMPT, ["invalid-a.wav", ""])
    audio = AudioController(sound_choice=lambda options: options[1])

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("selected empty array entry must not attempt file playback")

    monkeypatch.setattr(audio, "play_file", fail_if_called)

    assert audio.resolve_sound_path(cfg, SoundEvent.INVALID_PROMPT) is None
    await audio.play_sound_event(cfg, SoundEvent.INVALID_PROMPT)


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


async def test_looping_sound_event_configured_as_array_selects_once_per_loop_session(monkeypatch, tmp_path):
    cfg = _config_with_event_file(tmp_path, SoundEvent.THINKING, ["thinking-a.wav", "thinking-b.wav"])
    choices: list[list[str]] = []
    played_paths: list[Path] = []

    def choose(options):
        choices.append(list(options))
        return options[1]

    audio = AudioController(sound_choice=choose)
    first_play_started = asyncio.Event()

    async def record_play_file(cfg_arg, path, *, cancel_event=None, **kwargs):
        played_paths.append(Path(path))
        first_play_started.set()
        if cancel_event:
            await cancel_event.wait()

    monkeypatch.setattr(audio, "play_file", record_play_file)

    handle = audio.start_looping_sound(cfg, SoundEvent.THINKING)
    await asyncio.wait_for(first_play_started.wait(), timeout=3)
    await handle.stop()

    assert choices == [["thinking-a.wav", "thinking-b.wav"]]
    assert played_paths == [Path(cfg.sounds.library_dir) / "thinking-b.wav"]


async def test_empty_command_thinking_sound_is_safe_noop(monkeypatch, tmp_path):
    cfg = _config_with_event_file(tmp_path, SoundEvent.COMMAND_THINKING, "")
    audio = AudioController()

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("empty command_thinking must not attempt file playback")

    monkeypatch.setattr(audio, "play_file", fail_if_called)

    assert audio.resolve_sound_path(cfg, SoundEvent.COMMAND_THINKING) is None
    handle = audio.start_looping_sound(cfg, SoundEvent.COMMAND_THINKING)
    await asyncio.sleep(0)
    assert handle.task.done()
    await handle.stop()


def test_config_persistence_preserves_empty_and_array_sound_event_files(tmp_path):
    store = ConfigStore(tmp_path / "config.json")
    data = store.get_saved().public_dict()
    data["sounds"]["event_files"][SoundEvent.PROMPT_ACCEPTED.value] = ""
    data["sounds"]["event_files"][SoundEvent.INVALID_PROMPT.value] = ["invalid-a.wav", "invalid-b.wav"]

    result = store.apply_config(data)

    assert result.saved["sounds"]["event_files"][SoundEvent.PROMPT_ACCEPTED.value] == ""
    assert result.saved["sounds"]["event_files"][SoundEvent.INVALID_PROMPT.value] == ["invalid-a.wav", "invalid-b.wav"]
    reloaded = ConfigStore(tmp_path / "config.json")
    assert reloaded.get_saved().sounds.event_files[SoundEvent.PROMPT_ACCEPTED] == ""
    assert reloaded.get_saved().sounds.event_files[SoundEvent.INVALID_PROMPT] == ["invalid-a.wav", "invalid-b.wav"]
