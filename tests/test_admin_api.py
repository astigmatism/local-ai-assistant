from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from voice_assistant.app import RuntimeBundle, create_app
from voice_assistant.constants import EventType, SoundEvent


def make_client(bundle_parts):
    store, telemetry, runtime, audio, stt, llm, tts = bundle_parts
    app = create_app(RuntimeBundle(store, telemetry, runtime))
    app.state.disable_runtime_start = True
    return TestClient(app), (store, telemetry, runtime, audio, stt, llm, tts)


def test_admin_portal_and_status_need_no_auth(bundle_parts):
    client, _ = make_client(bundle_parts)
    index = client.get("/")
    assert index.status_code == 200
    assert "empty string" in index.text
    response = client.get("/api/status")
    assert response.status_code == 200
    assert "state" in response.json()


def test_config_draft_apply_export_import_and_restart_pending(bundle_parts):
    client, (store, telemetry, *_rest) = make_client(bundle_parts)
    snapshot = client.get("/api/config").json()
    saved = snapshot["saved"]
    saved["prompt_capture"]["silence_duration_seconds"] = 1.7
    saved["services"]["llm"]["url"] = "http://new-router.local/api/chat"
    assert client.post("/api/config/draft", json=saved).status_code == 200
    applied = client.post("/api/config/apply", json={}).json()
    assert applied["active"]["prompt_capture"]["silence_duration_seconds"] == 1.7
    assert applied["active"]["services"]["llm"]["url"] != "http://new-router.local/api/chat"
    assert applied["saved"]["services"]["llm"]["url"] == "http://new-router.local/api/chat"
    assert "services.llm.url" in applied["pending_restart_paths"]
    exported = client.get("/api/config/export").json()
    assert exported["services"]["llm"]["url"] == "http://new-router.local/api/chat"
    assert client.post("/api/config/import", json=exported).status_code == 200


def test_config_export_import_preserves_empty_sound_event_file(bundle_parts):
    client, _ = make_client(bundle_parts)
    saved = client.get("/api/config").json()["saved"]
    saved["sounds"]["event_files"][SoundEvent.PROMPT_ACCEPTED.value] = ""

    applied = client.post("/api/config/apply", json={"config": saved})

    assert applied.status_code == 200
    assert applied.json()["saved"]["sounds"]["event_files"][SoundEvent.PROMPT_ACCEPTED.value] == ""
    exported = client.get("/api/config/export").json()
    assert exported["sounds"]["event_files"][SoundEvent.PROMPT_ACCEPTED.value] == ""
    imported = client.post("/api/config/import", json=exported)
    assert imported.status_code == 200
    assert imported.json()["draft"]["sounds"]["event_files"][SoundEvent.PROMPT_ACCEPTED.value] == ""


def test_sound_upload_list_play_delete(bundle_parts, tmp_path):
    client, (store, telemetry, runtime, audio, *_rest) = make_client(bundle_parts)
    sound = tmp_path / "custom.wav"
    sound.write_bytes(b"RIFFxxxxWAVEfmt ")
    with sound.open("rb") as handle:
        response = client.post("/api/sounds", files={"file": ("custom.wav", handle, "audio/wav")})
    assert response.status_code == 200
    sounds_payload = client.get("/api/sounds").json()
    assert "custom.wav" in sounds_payload["files"]
    assert "empty string" in sounds_payload["format_guidance"]
    assert client.post("/api/sounds/custom.wav/play").status_code == 200
    assert client.delete("/api/sounds/custom.wav").status_code == 200
    assert "custom.wav" not in client.get("/api/sounds").json()["files"]


def test_telemetry_search_and_live_history_endpoint(bundle_parts):
    client, (store, telemetry, *_rest) = make_client(bundle_parts)
    telemetry.log_event(EventType.ADMIN_TEST, "A searchable event", component="admin", data={"token": "needle"})
    found = client.get("/api/telemetry/events?search=needle").json()["events"]
    assert found and found[0]["human_message"] == "A searchable event"


def test_microphone_test_records_artifact_when_enabled(bundle_parts):
    client, (store, telemetry, runtime, audio, *_rest) = make_client(bundle_parts)
    response = client.post("/api/test/microphone", json={"duration_seconds": 1})
    assert response.status_code == 200
    body = response.json()
    assert body["artifact"] is not None
    artifact_id = body["artifact"]["id"]
    assert client.get(f"/api/artifacts/{artifact_id}/download").status_code == 200


def test_command_recognition_admin_test_uses_whole_utterance(bundle_parts):
    client, _ = make_client(bundle_parts)
    assert client.post("/api/test/command-recognition", json={"text": "stop"}).json()["matched"]["intent"] == "cancel_stop"
    assert client.post("/api/test/command-recognition", json={"text": "How do I stop a process?"}).json()["matched"] is None


def test_maintenance_requires_confirmation_and_is_safe_by_default(bundle_parts):
    client, _ = make_client(bundle_parts)
    no_confirm = client.post("/api/maintenance/restart-service", json={"confirm": False}).json()
    assert no_confirm["accepted"] is False
    confirmed = client.post("/api/maintenance/restart-service", json={"confirm": True}).json()
    assert confirmed["accepted"] is True
    assert confirmed["executed"] is False
    assert "disabled" in confirmed["output"]


def test_admin_simulated_wake_is_labeled_diagnostic_only(bundle_parts):
    client, _ = make_client(bundle_parts)
    response = client.post("/api/test/wake", json={"confidence": 1.0})
    assert response.status_code == 200
    body = response.json()
    assert body["diagnostic_only"] is True
    assert "production" in body["message"]


def test_admin_migrate_production_wake_endpoint_preserves_other_settings(bundle_parts):
    client, (store, telemetry, *_rest) = make_client(bundle_parts)
    saved = store.get_saved().public_dict()
    saved["wake"]["engine"] = "simulated"
    saved["wake"]["external_command"] = []
    saved["wake"]["external_health_command"] = []
    saved["prompt_capture"]["silence_duration_seconds"] = 1.9
    store.apply_config(saved)

    rejected = client.post("/api/config/migrate-production-wake", json={"confirm": False})
    assert rejected.status_code == 400

    response = client.post("/api/config/migrate-production-wake", json={"confirm": True})
    assert response.status_code == 200
    body = response.json()
    assert body["saved"]["wake"]["engine"] == "external_command"
    assert body["saved"]["wake"]["external_command"] == ["python", "-m", "voice_assistant.pocketsphinx_wake"]
    assert body["saved"]["prompt_capture"]["silence_duration_seconds"] == 1.9


def test_wake_debug_endpoint_distinguishes_production_from_simulated_admin(bundle_parts):
    client, _ = make_client(bundle_parts)
    body = client.get("/api/wake/debug").json()
    assert body["wake_status"]["configured_engine"] == "external_command"
    assert body["simulated_admin_endpoint"] == "/api/test/wake"
    assert "not the simulated" in body["production_note"]
