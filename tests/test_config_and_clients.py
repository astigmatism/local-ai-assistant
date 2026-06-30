from __future__ import annotations

from voice_assistant.clients import LLMClient
from voice_assistant.config import AssistantConfig, ConfigStore
from voice_assistant.constants import CommandIntent, SoundEvent


def test_default_configuration_matches_design_inventory(tmp_path):
    cfg = AssistantConfig()
    assert cfg.prompt_capture.minimum_duration_seconds == 3.0
    assert cfg.prompt_capture.maximum_duration_seconds == 120.0
    assert cfg.conversation.inactivity_timeout_seconds == 60.0
    assert cfg.telemetry.retention_days == 365
    assert cfg.telemetry.cleanup_interval == "daily"
    assert cfg.wake.active_wake_phrase in cfg.wake.wake_phrases
    assert {command.intent for command in cfg.command_registry.commands} == {
        CommandIntent.CANCEL_STOP,
        CommandIntent.NEW_CONVERSATION,
    }
    assert set(cfg.sounds.event_files) == set(SoundEvent)


def test_restart_required_service_settings_are_saved_but_not_runtime_active(tmp_path):
    store = ConfigStore(tmp_path / "config.json")
    original_active_url = store.get_active().services.llm.url
    data = store.get_saved().public_dict()
    data["services"]["llm"]["url"] = "http://example.local:11434/api/chat"
    data["conversation"]["inactivity_timeout_seconds"] = 42
    result = store.apply_config(data)
    assert store.get_saved().services.llm.url == "http://example.local:11434/api/chat"
    assert store.get_active().services.llm.url == original_active_url
    assert store.get_active().conversation.inactivity_timeout_seconds == 42
    assert "services.llm.url" in result.pending_restart_paths
    assert "conversation.inactivity_timeout_seconds" in result.applied_runtime_paths


def test_config_draft_import_and_apply(tmp_path):
    store = ConfigStore(tmp_path / "config.json")
    imported = store.get_saved().public_dict()
    imported["prompt_capture"]["silence_duration_seconds"] = 2.5
    draft = store.import_to_draft(imported)
    assert draft.prompt_capture.silence_duration_seconds == 2.5
    result = store.apply_draft()
    assert result.active["prompt_capture"]["silence_duration_seconds"] == 2.5
    assert not store.draft_path.exists()


def test_llm_client_never_sends_model_field():
    cfg = AssistantConfig().services.llm
    payload = LLMClient(cfg).build_payload([{"role": "user", "content": "hello"}])
    assert payload["stream"] is False
    assert payload["messages"][0]["content"] == "hello"
    assert "model" not in payload


async def test_llm_client_post_to_router_omits_model_field(monkeypatch):
    import httpx

    captured = {}

    class RecordingAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json):
            captured["url"] = url
            captured["json"] = json
            request = httpx.Request("POST", url)
            return httpx.Response(200, json={"message": {"content": "ok"}}, request=request)

    monkeypatch.setattr(httpx, "AsyncClient", RecordingAsyncClient)
    cfg = AssistantConfig().services.llm
    text = await LLMClient(cfg).chat([{"role": "user", "content": "hello"}])
    assert text == "ok"
    assert captured["url"] == cfg.url
    assert "model" not in captured["json"]
