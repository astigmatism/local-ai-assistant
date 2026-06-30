from __future__ import annotations

from datetime import timedelta

from voice_assistant.commands import CommandRegistry
from voice_assistant.config import AssistantConfig
from voice_assistant.constants import CommandIntent
from voice_assistant.conversation import ConversationManager
from voice_assistant.telemetry import utc_now


def test_command_matching_uses_whole_utterance_not_substrings():
    registry = CommandRegistry(AssistantConfig().command_registry)
    assert registry.match_text("cancel").intent == CommandIntent.CANCEL_STOP.value
    assert registry.match_text("Cancel!").intent == CommandIntent.CANCEL_STOP.value
    assert registry.match_text("How do I cancel a process in Linux?") is None
    assert registry.match_text("please stop talking") is None


def test_command_aliases_and_disabled_state():
    cfg = AssistantConfig()
    registry = CommandRegistry(cfg.command_registry)
    assert registry.match_text("never mind").intent == CommandIntent.CANCEL_STOP.value
    assert registry.match_text("start over").intent == CommandIntent.NEW_CONVERSATION.value
    cfg.command_registry.commands[0].enabled = False
    registry = CommandRegistry(cfg.command_registry)
    assert registry.match_text("stop") is None


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
