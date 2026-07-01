from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

import voice_assistant.commands as commands_module
from conftest import write_wav
from voice_assistant.commands import CommandRegistry, PocketsphinxCommandRecognizer, build_command_recognizer
from voice_assistant.config import AssistantConfig
from voice_assistant.constants import CommandIntent
from voice_assistant.conversation import ConversationManager
from voice_assistant.telemetry import utc_now


@pytest.mark.parametrize(
    "phrase",
    ["stop", "cancel", "forget it", "never mind", " Stop. ", "CANCEL", "Never mind!"],
)
def test_cancel_stop_aliases_match_whole_command_utterance_variants(phrase):
    registry = CommandRegistry(AssistantConfig().command_registry)

    match = registry.match_text(phrase)

    assert match is not None
    assert match.intent == CommandIntent.CANCEL_STOP.value


@pytest.mark.parametrize(
    "phrase",
    ["How do I stop a Linux service?", "How do I cancel a process in Linux?", "please stop talking"],
)
def test_command_matching_uses_whole_utterance_not_substrings(phrase):
    registry = CommandRegistry(AssistantConfig().command_registry)

    assert registry.match_text(phrase) is None



def test_command_aliases_and_disabled_state():
    cfg = AssistantConfig()
    registry = CommandRegistry(cfg.command_registry)
    assert registry.match_text("never mind").intent == CommandIntent.CANCEL_STOP.value
    assert registry.match_text("start over").intent == CommandIntent.NEW_CONVERSATION.value
    cfg.command_registry.commands[0].enabled = False
    registry = CommandRegistry(cfg.command_registry)
    assert registry.match_text("stop") is None



def test_default_command_recognizer_is_local_audio_recognizer_not_diagnostic_text_only():
    cfg = AssistantConfig()

    recognizer = build_command_recognizer(cfg.command_registry)

    assert cfg.command_registry.recognizer.engine == "pocketsphinx"
    assert isinstance(recognizer, PocketsphinxCommandRecognizer)


class _FakePocketsphinxProcess:
    def __init__(self, stdout: bytes, stderr: bytes = b"", returncode: int = 0):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self.terminated = False
        self.killed = False

    async def communicate(self, input=None):
        self.input = input
        return self._stdout, self._stderr

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        return self.returncode


@pytest.mark.asyncio
async def test_pocketsphinx_recognizer_decodes_prompt_audio_locally_before_matching(monkeypatch, tmp_path):
    wav_path = write_wav(tmp_path / "prompt.wav")
    registry = CommandRegistry(AssistantConfig().command_registry)
    captured_command: list[str] = []

    async def fake_create_subprocess_exec(*command, stdin=None, stdout=None, stderr=None):
        captured_command.extend(command)
        return _FakePocketsphinxProcess(b"000000000: Cancel.\n")

    monkeypatch.setattr(commands_module.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(commands_module.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    recognizer = PocketsphinxCommandRecognizer(
        command=["pocketsphinx_continuous"],
        hmm_path="/model/hmm",
        dict_path="/model/dict",
        lm_path="/model/lm.bin",
        timeout_seconds=1.0,
    )

    match = await recognizer.recognize(wav_path, registry)

    assert match is not None
    assert match.intent == CommandIntent.CANCEL_STOP.value
    assert match.alias == "cancel"
    assert captured_command[:1] == ["pocketsphinx_continuous"]
    assert "-infile" in captured_command
    assert "/dev/stdin" in captured_command
    assert str(wav_path) not in captured_command
    assert "-lm" in captured_command


@pytest.mark.asyncio
async def test_pocketsphinx_recognizer_rejects_longer_local_transcripts_with_command_words(monkeypatch, tmp_path):
    wav_path = write_wav(tmp_path / "prompt.wav")
    registry = CommandRegistry(AssistantConfig().command_registry)

    async def fake_create_subprocess_exec(*command, stdin=None, stdout=None, stderr=None):
        return _FakePocketsphinxProcess(b"000000000: How do I stop a Linux service?\n")

    monkeypatch.setattr(commands_module.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(commands_module.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    recognizer = PocketsphinxCommandRecognizer(
        command=["pocketsphinx_continuous"],
        hmm_path="/model/hmm",
        dict_path="/model/dict",
        lm_path=None,
        timeout_seconds=1.0,
    )

    assert await recognizer.recognize(wav_path, registry) is None




@pytest.mark.asyncio
async def test_pocketsphinx_keyphrase_fallback_catches_short_cancel_when_language_model_misses(monkeypatch, tmp_path):
    wav_path = write_wav(tmp_path / "prompt.wav", duration=0.45)
    registry = CommandRegistry(AssistantConfig().command_registry)
    captured_commands: list[tuple[str, ...]] = []

    async def fake_create_subprocess_exec(*command, stdin=None, stdout=None, stderr=None):
        captured_commands.append(tuple(command))
        if "-lm" in command:
            return _FakePocketsphinxProcess(b"000000000: council\n")
        if "-kws" in command:
            kws_path = command[command.index("-kws") + 1]
            assert "cancel" in Path(kws_path).read_text(encoding="utf-8")
            return _FakePocketsphinxProcess(b"000000000: cancel\n")
        raise AssertionError(f"unexpected PocketSphinx command: {command}")

    monkeypatch.setattr(commands_module.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(commands_module.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    recognizer = PocketsphinxCommandRecognizer(
        command=["pocketsphinx_continuous"],
        hmm_path="/model/hmm",
        dict_path="/model/dict",
        lm_path="/model/lm.bin",
        timeout_seconds=1.0,
    )

    match = await recognizer.recognize(wav_path, registry)

    assert match is not None
    assert match.intent == CommandIntent.CANCEL_STOP.value
    assert match.alias == "cancel"
    assert any("-kws" in command for command in captured_commands)
    assert recognizer.last_diagnostics["path"] == "keyphrase"
    assert recognizer.last_diagnostics["matched"] is True


@pytest.mark.asyncio
async def test_pocketsphinx_keyphrase_fallback_rejects_keyword_inside_longer_language_candidate(monkeypatch, tmp_path):
    wav_path = write_wav(tmp_path / "prompt.wav", duration=0.45)
    registry = CommandRegistry(AssistantConfig().command_registry)

    async def fake_create_subprocess_exec(*command, stdin=None, stdout=None, stderr=None):
        if "-lm" in command:
            return _FakePocketsphinxProcess(b"000000000: How do I cancel a process in Linux?\n")
        if "-kws" in command:
            return _FakePocketsphinxProcess(b"000000000: cancel\n")
        raise AssertionError(f"unexpected PocketSphinx command: {command}")

    monkeypatch.setattr(commands_module.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(commands_module.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    recognizer = PocketsphinxCommandRecognizer(
        command=["pocketsphinx_continuous"],
        hmm_path="/model/hmm",
        dict_path="/model/dict",
        lm_path="/model/lm.bin",
        timeout_seconds=1.0,
    )

    assert await recognizer.recognize(wav_path, registry) is None
    assert recognizer.last_diagnostics["reason"] == "language_model_heard_more_than_command_alias"


@pytest.mark.asyncio
async def test_pocketsphinx_keyphrase_fallback_rejects_long_active_speech_span(monkeypatch, tmp_path):
    wav_path = write_wav(tmp_path / "prompt.wav", duration=1.8)
    registry = CommandRegistry(AssistantConfig().command_registry)

    async def fake_create_subprocess_exec(*command, stdin=None, stdout=None, stderr=None):
        if "-lm" in command:
            return _FakePocketsphinxProcess(b"")
        if "-kws" in command:
            return _FakePocketsphinxProcess(b"000000000: cancel\n")
        raise AssertionError(f"unexpected PocketSphinx command: {command}")

    monkeypatch.setattr(commands_module.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(commands_module.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    recognizer = PocketsphinxCommandRecognizer(
        command=["pocketsphinx_continuous"],
        hmm_path="/model/hmm",
        dict_path="/model/dict",
        lm_path="/model/lm.bin",
        timeout_seconds=1.0,
    )

    assert await recognizer.recognize(wav_path, registry) is None
    assert recognizer.last_diagnostics["reason"] == "speech_span_too_long_for_command_alias"


@pytest.mark.asyncio
async def test_pocketsphinx_recognizer_uses_sidecar_text_without_external_decoder(monkeypatch, tmp_path):
    wav_path = write_wav(tmp_path / "prompt.wav")
    sidecar = tmp_path / "prompt.wav.command.txt"
    sidecar.write_text("forget it", encoding="utf-8")
    registry = CommandRegistry(AssistantConfig().command_registry)

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("sidecar command text should be matched without running a decoder")

    monkeypatch.setattr(commands_module.asyncio, "create_subprocess_exec", fail_if_called)
    recognizer = PocketsphinxCommandRecognizer(
        command=["pocketsphinx_continuous"],
        hmm_path="/model/hmm",
        dict_path="/model/dict",
        lm_path=None,
        timeout_seconds=1.0,
    )

    match = await recognizer.recognize(wav_path, registry)

    assert match is not None
    assert match.intent == CommandIntent.CANCEL_STOP.value
    assert match.alias == "forget it"



def test_conversation_preserves_context_until_timeout_and_does_not_truncate():
    manager = ConversationManager("system", inactivity_timeout_seconds=60)
    cid = manager.conversation_id
    for i in range(50):
        manager.add_user(f"u{i}")
        manager.add_assistant(f"a{i}")
    assert manager.conversation_id == cid
    assert len(manager.messages_for_llm()) == 101
    manager.mark_response_finished(utc_now() - timedelta(seconds=30))
    assert manager.expire_if_needed(utc_now()) is False
    assert manager.conversation_id == cid
    manager.mark_response_finished(utc_now() - timedelta(seconds=61))
    assert manager.expire_if_needed(utc_now()) is True
    assert manager.conversation_id != cid
    assert manager.messages_for_llm() == [{"role": "system", "content": "system"}]



def test_new_conversation_reset_discards_context():
    manager = ConversationManager("system", inactivity_timeout_seconds=60)
    old = manager.conversation_id
    manager.add_user("hello")
    new = manager.reset()
    assert new != old
    assert manager.messages_for_llm() == [{"role": "system", "content": "system"}]
