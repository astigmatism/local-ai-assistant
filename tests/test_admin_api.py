from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from voice_assistant.app import RuntimeBundle, create_app
from voice_assistant.constants import EventType, SoundEvent


ADMIN_SECTION_PATTERN = re.compile(
    r'<section class="admin-panel" data-admin-section="(?P<key>[^"]+)">\s*'
    r'<h2>\s*<button (?P<button_attrs>[^>]*?)>\s*(?P<button_inner>.*?)</button>\s*</h2>\s*'
    r'<div (?P<body_attrs>[^>]*?)>\s*'
    r'<div (?P<content_attrs>[^>]*?)>\s*(?P<body>.*?)\s*</div>\s*'
    r'<span (?P<resize_attrs>[^>]*?)>(?P<resize_text>.*?)</span>\s*'
    r'</div>\s*</section>',
    re.S,
)
ATTR_PATTERN = re.compile(r'([A-Za-z_:][-A-Za-z0-9_:.]*)(?:="([^"]*)")?')
KNOWN_ADMIN_SECTION_TITLES = {"Status", "Configuration", "Sound Library", "Diagnostics", "Telemetry"}


def _attrs(fragment: str) -> dict[str, str]:
    return {name: value for name, value in ATTR_PATTERN.findall(fragment)}


def _admin_panels(html: str) -> list[dict[str, object]]:
    panels = []
    for match in ADMIN_SECTION_PATTERN.finditer(html):
        title_match = re.search(r'<span>(?P<title>.*?)</span>', match.group('button_inner'), re.S)
        assert title_match is not None
        panels.append(
            {
                "key": match.group('key'),
                "title": title_match.group('title').strip(),
                "button_attrs": _attrs(match.group('button_attrs')),
                "button_inner": match.group('button_inner'),
                "body_attrs": _attrs(match.group('body_attrs')),
                "body": match.group('body'),
                "content_attrs": _attrs(match.group('content_attrs')),
                "resize_attrs": _attrs(match.group('resize_attrs')),
                "resize_text": match.group('resize_text').strip(),
            }
        )
    return panels


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


def test_admin_portal_top_level_sections_are_collapsed_by_default(bundle_parts):
    client, _ = make_client(bundle_parts)
    html = client.get("/").text
    panels = _admin_panels(html)
    titles = {str(panel["title"]) for panel in panels}

    assert KNOWN_ADMIN_SECTION_TITLES <= titles
    assert html.count('<section') == len(panels)
    for panel in panels:
        button_attrs = panel["button_attrs"]
        body_attrs = panel["body_attrs"]
        content_attrs = panel["content_attrs"]
        resize_attrs = panel["resize_attrs"]
        assert isinstance(button_attrs, dict)
        assert isinstance(body_attrs, dict)
        assert isinstance(content_attrs, dict)
        assert isinstance(resize_attrs, dict)
        assert button_attrs["type"] == "button"
        assert button_attrs["aria-expanded"] == "false"
        assert button_attrs["onclick"] == "togglePanel(this)"
        assert button_attrs["aria-controls"] == body_attrs["id"]
        assert "admin-panel-body" in body_attrs["class"].split()
        assert "data-resizable-panel" in body_attrs
        assert body_attrs["role"] == "region"
        assert body_attrs["aria-labelledby"] == button_attrs["id"]
        assert "hidden" in body_attrs
        assert "admin-panel-body-content" in content_attrs["class"].split()
        assert "data-panel-content" in content_attrs
        assert "admin-panel-resize-handle" in resize_attrs["class"].split()
        assert "data-resize-handle" in resize_attrs
        assert resize_attrs["role"] == "separator"
        assert resize_attrs["aria-orientation"] == "horizontal"
        assert resize_attrs["tabindex"] == "0"
        assert panel["title"] in resize_attrs["aria-label"]


def test_known_admin_sections_keep_their_content_inside_collapsible_bodies(bundle_parts):
    client, _ = make_client(bundle_parts)
    panels = {str(panel["title"]): str(panel["body"]) for panel in _admin_panels(client.get("/").text)}

    expected_content = {
        "Status": ['id="status"', 'id="health"', 'loadWakeDebug()'],
        "Configuration": ['id="config"', 'applyDraft()', 'Edits are applied as a group'],
        "Sound Library": ['id="soundFile"', 'loadSounds()', 'empty string'],
        "Diagnostics": ['id="testText"', 'llmTtsTest()', 'commandTest()'],
        "Telemetry": ['id="events"', 'loadEvents()', 'Search history'],
    }
    for title, tokens in expected_content.items():
        assert title in panels
        for token in tokens:
            assert token in panels[title]


def test_admin_portal_sections_are_resizable_scrollable_panels(bundle_parts):
    client, _ = make_client(bundle_parts)
    html = client.get("/").text
    panels = _admin_panels(html)

    assert ".admin-panel-body" in html
    assert "height: 20rem" in html
    assert "min-height: 12rem" in html
    assert "max-height: min(75vh, 48rem)" in html
    assert "resize: vertical" in html
    assert "overflow: hidden" in html
    assert ".admin-panel-body-content" in html
    assert "overflow: auto" in html
    assert ".admin-panel-body[hidden] { display: none; }" in html

    assert len(panels) == html.count('class="admin-panel-resize-handle" data-resize-handle')
    for panel in panels:
        resize_attrs = panel["resize_attrs"]
        assert isinstance(resize_attrs, dict)
        assert resize_attrs["aria-valuemin"] == "180"
        assert resize_attrs["aria-valuemax"] == "720"
        assert resize_attrs["aria-valuenow"] == "320"
        assert "Drag or use arrow keys to resize" == panel["resize_text"]


def test_admin_portal_toggle_reveals_and_hides_without_replacing_form_state(bundle_parts):
    client, _ = make_client(bundle_parts)
    html = client.get("/").text

    assert "function togglePanel(button)" in html
    assert "const nextExpanded = button.getAttribute('aria-expanded') !== 'true';" in html
    assert "button.setAttribute('aria-expanded', String(nextExpanded));" in html
    assert "body.hidden = !nextExpanded;" in html
    assert "section.dataset.expanded = String(nextExpanded);" in html
    assert "if(nextExpanded && body){ updateResizeHandleValue(body); }" in html
    assert "state.textContent = nextExpanded ? 'Collapse -' : 'Expand +';" in html
    assert ".innerHTML" not in html

    config_panel = next(panel for panel in _admin_panels(html) if panel["title"] == "Configuration")
    assert 'textarea id="config"' in str(config_panel["body"])
    assert 'onclick="applyDraft()"' in str(config_panel["body"])


def test_admin_portal_resize_script_changes_only_the_target_panel_height(bundle_parts):
    client, _ = make_client(bundle_parts)
    html = client.get("/").text

    assert "function setPanelHeight(body, height)" in html
    assert "body.style.height = clampPanelHeight(height) + 'px';" in html
    assert "function beginPanelResize(event)" in html
    assert "const body = handle.closest('.admin-panel-body');" in html
    assert "setPanelHeight(body, startHeight + moveEvent.clientY - startY);" in html
    assert "document.addEventListener('pointermove', resizePanel);" in html
    assert "document.removeEventListener('pointermove', resizePanel);" in html
    assert "initializeResizablePanels();" in html


def test_admin_portal_resize_handle_supports_keyboard_adjustment(bundle_parts):
    client, _ = make_client(bundle_parts)
    html = client.get("/").text

    assert "handle.addEventListener('keydown', resizePanelFromKeyboard);" in html
    assert "event.key === 'ArrowDown'" in html
    assert "event.key === 'ArrowUp'" in html
    assert "event.key === 'Home'" in html
    assert "event.key === 'End'" in html


def test_admin_portal_collapsible_controls_are_keyboard_accessible_buttons(bundle_parts):
    client, _ = make_client(bundle_parts)
    for panel in _admin_panels(client.get("/").text):
        button_attrs = panel["button_attrs"]
        assert isinstance(button_attrs, dict)
        assert button_attrs["type"] == "button"
        assert "admin-panel-toggle" in button_attrs["class"].split()
        assert button_attrs["aria-expanded"] in {"false", "true"}
        assert "data-panel-state" in str(panel["button_inner"])


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
