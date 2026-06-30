from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from voice_assistant.app import RuntimeBundle, create_app
from voice_assistant.constants import EventType, SoundEvent


SECTION_START_PATTERN = re.compile(r'<section\s+(?P<attrs>[^>]*data-admin-section="(?P<key>[^"]+)"[^>]*)>', re.S)
ATTR_PATTERN = re.compile(r'([A-Za-z_:][-A-Za-z0-9_:.]*)(?:="([^"]*)")?')
KNOWN_ADMIN_SECTION_TITLES = {"Status", "Configuration", "Sound Library", "Diagnostics", "Telemetry"}
LAZY_LOADERS = {
    "status": "loadStatus",
    "configuration": "loadConfig",
    "sound-library": "loadSounds",
    "telemetry": "loadEvents",
}


def _attrs(fragment: str) -> dict[str, str]:
    return {name: value for name, value in ATTR_PATTERN.findall(fragment)}


def _class_names(attrs: dict[str, str]) -> set[str]:
    return set(attrs.get("class", "").split())


def _first_tag_attrs(fragment: str, tag: str, *, element_id: str | None = None, class_name: str | None = None) -> dict[str, str]:
    for match in re.finditer(rf'<{tag}\s+(?P<attrs>[^>]*)>', fragment, re.S):
        attrs = _attrs(match.group("attrs"))
        if element_id is not None and attrs.get("id") != element_id:
            continue
        if class_name is not None and class_name not in _class_names(attrs):
            continue
        return attrs
    raise AssertionError(f"Could not find <{tag}> in admin panel fragment")


def _section_fragments(html: str) -> list[tuple[str, dict[str, str], str]]:
    matches = list(SECTION_START_PATTERN.finditer(html))
    fragments: list[tuple[str, dict[str, str], str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else html.index("<script>", match.end())
        fragments.append((match.group("key"), _attrs(match.group("attrs")), html[match.start() : end]))
    return fragments


def _admin_panels(html: str) -> list[dict[str, object]]:
    panels: list[dict[str, object]] = []
    for key, section_attrs, fragment in _section_fragments(html):
        title_match = re.search(r'<span class="admin-panel-title">(?P<title>.*?)</span>', fragment, re.S)
        assert title_match is not None
        panels.append(
            {
                "key": key,
                "title": title_match.group("title").strip(),
                "section_attrs": section_attrs,
                "button_attrs": _first_tag_attrs(fragment, "button", element_id=f"toggle-{key}"),
                "body_attrs": _first_tag_attrs(fragment, "div", element_id=f"panel-{key}"),
                "handle_attrs": _first_tag_attrs(fragment, "div", class_name="admin-panel-resize-handle"),
                "fragment": fragment,
            }
        )
    return panels


def _style_block(html: str) -> str:
    return html.split("<style>", 1)[1].split("</style>", 1)[0]


def _script_block(html: str) -> str:
    return html.split("<script>", 1)[1].split("</script>", 1)[0]


def make_client(bundle_parts):
    store, telemetry, runtime, audio, stt, llm, tts = bundle_parts
    app = create_app(RuntimeBundle(store, telemetry, runtime))
    app.state.disable_runtime_start = True
    return TestClient(app), (store, telemetry, runtime, audio, stt, llm, tts)


def test_admin_portal_and_status_need_no_auth(bundle_parts):
    client, _ = make_client(bundle_parts)
    index = client.get("/")
    assert index.status_code == 200
    assert index.headers["cache-control"] == "no-store, max-age=0"
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
    assert html.count('<section class="admin-panel') == len(panels)
    for panel in panels:
        section_attrs = panel["section_attrs"]
        button_attrs = panel["button_attrs"]
        body_attrs = panel["body_attrs"]
        assert isinstance(section_attrs, dict)
        assert isinstance(button_attrs, dict)
        assert isinstance(body_attrs, dict)
        assert "admin-panel" in _class_names(section_attrs)
        assert "is-collapsed" in _class_names(section_attrs)
        assert "is-expanded" not in _class_names(section_attrs)
        assert button_attrs["type"] == "button"
        assert button_attrs["aria-expanded"] == "false"
        assert button_attrs["onclick"] == "togglePanel(this)"
        assert button_attrs["aria-controls"] == body_attrs["id"]
        assert body_attrs["role"] == "region"
        assert body_attrs["aria-labelledby"] == button_attrs["id"]
        assert body_attrs["aria-hidden"] == "true"
        assert "hidden" in body_attrs
        assert "Collapsed - click to expand" in str(panel["fragment"])


def test_known_admin_sections_keep_their_content_inside_collapsible_bodies(bundle_parts):
    client, _ = make_client(bundle_parts)
    panels = {str(panel["title"]): str(panel["fragment"]) for panel in _admin_panels(client.get("/").text)}

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


def test_admin_portal_runtime_content_is_not_loaded_until_panel_expands(bundle_parts):
    client, _ = make_client(bundle_parts)
    html = client.get("/").text
    script = _script_block(html)
    initializer = script.split("function initializeAdminPortal(){", 1)[1].split("}\nif(document.readyState", 1)[0]
    panels = {str(panel["key"]): panel for panel in _admin_panels(html)}

    assert "loadStatus(); loadConfig(); loadEvents(); loadSounds();" not in script
    assert "loadStatus();" not in initializer
    assert "loadConfig();" not in initializer
    assert "loadEvents();" not in initializer
    assert "loadSounds();" not in initializer
    assert "loadPanelOnFirstExpand(section, body);" in script
    assert "body.dataset.loaded = 'true';" in script
    for key, loader in LAZY_LOADERS.items():
        body_attrs = panels[key]["body_attrs"]
        assert isinstance(body_attrs, dict)
        assert body_attrs["data-load-on-expand"] == loader
    diagnostics_attrs = panels["diagnostics"]["body_attrs"]
    assert isinstance(diagnostics_attrs, dict)
    assert "data-load-on-expand" not in diagnostics_attrs


def test_admin_portal_collapsed_layout_hides_runtime_populated_targets(bundle_parts):
    client, _ = make_client(bundle_parts)
    html = client.get("/").text
    style = _style_block(html)
    script = _script_block(html)
    status_panel = next(panel for panel in _admin_panels(html) if panel["title"] == "Status")

    assert "[hidden] { display: none !important; }" in style
    assert "section.admin-panel.is-collapsed > .admin-panel-body" in style
    assert ".admin-panel-body[hidden]" in style
    assert "display: none !important" in style
    assert "height: 0 !important" in style
    assert "overflow: hidden !important" in style
    assert "visibility: hidden !important" in style
    assert "section.admin-panel.is-collapsed [data-runtime-output]" in style
    assert 'id="panel-status"' in str(status_panel["fragment"])
    assert 'id="status" data-runtime-output' in str(status_panel["fragment"])
    assert str(status_panel["fragment"]).index('id="panel-status"') < str(status_panel["fragment"]).index('id="status"')
    assert "function keepCollapsedPanelHidden(target)" in script
    assert "function writeText(id, value)" in script
    assert "target.textContent = value;" in script
    assert "if(!section || !section.classList.contains('is-collapsed')){ return; }" in script
    assert "section.querySelector('[data-resizable-panel]')" in script
    assert "body.hidden = true;" in script
    assert "body.setAttribute('aria-hidden', 'true');" in script
    assert "async function loadStatus(){ writeJson('status', await j('/api/status')); }" in script
    assert "document.getElementById('status').textContent" not in script


def test_admin_portal_toggle_reveals_and_hides_without_replacing_form_state(bundle_parts):
    client, _ = make_client(bundle_parts)
    html = client.get("/").text

    assert "function togglePanel(button)" in html
    assert "function setPanelExpanded(button, expanded)" in html
    assert "const nextExpanded = button.getAttribute('aria-expanded') !== 'true';" in html
    assert "button.setAttribute('aria-expanded', String(expanded));" in html
    assert "body.hidden = !expanded;" in html
    assert "body.setAttribute('aria-hidden', String(!expanded));" in html
    assert "section.classList.toggle('is-collapsed', !expanded);" in html
    assert "state.textContent = expanded ? 'Expanded - click to collapse' : 'Collapsed - click to expand';" in html
    assert "loadPanelOnFirstExpand(section, body);" in html
    assert ".innerHTML" not in html
    assert "removeChild" not in html

    config_panel = next(panel for panel in _admin_panels(html) if panel["title"] == "Configuration")
    assert 'textarea id="config"' in str(config_panel["fragment"])
    assert 'onclick="applyDraft()"' in str(config_panel["fragment"])


def test_admin_portal_collapsible_controls_are_keyboard_accessible_buttons(bundle_parts):
    client, _ = make_client(bundle_parts)
    for panel in _admin_panels(client.get("/").text):
        button_attrs = panel["button_attrs"]
        assert isinstance(button_attrs, dict)
        assert button_attrs["type"] == "button"
        assert "admin-panel-toggle" in _class_names(button_attrs)
        assert button_attrs["aria-expanded"] in {"false", "true"}
        assert "data-panel-state" in str(panel["fragment"])
        assert "data-panel-marker" in str(panel["fragment"])


def test_admin_portal_all_sections_are_resizable_scroll_panels(bundle_parts):
    client, _ = make_client(bundle_parts)
    html = client.get("/").text
    style = _style_block(html)

    assert ".admin-panel-body-content" in style
    assert "overflow: auto" in style
    assert "max-height: min(75vh, 45rem)" in style
    for panel in _admin_panels(html):
        body_attrs = panel["body_attrs"]
        handle_attrs = panel["handle_attrs"]
        fragment = str(panel["fragment"])
        assert isinstance(body_attrs, dict)
        assert isinstance(handle_attrs, dict)
        assert "admin-panel-body" in _class_names(body_attrs)
        assert "data-resizable-panel" in body_attrs
        assert "data-panel-content" in fragment
        assert "admin-panel-resize-handle" in _class_names(handle_attrs)
        assert "data-resize-handle" in handle_attrs
        assert handle_attrs["role"] == "separator"
        assert handle_attrs["aria-orientation"] == "horizontal"
        assert handle_attrs["tabindex"] == "0"
        assert handle_attrs["aria-valuemin"] == "192"
        assert handle_attrs["aria-valuemax"] == "720"
        assert handle_attrs["aria-valuenow"] == "320"


def test_admin_portal_resize_pointer_and_keyboard_wiring(bundle_parts):
    client, _ = make_client(bundle_parts)
    html = client.get("/").text

    assert "function beginPanelResize(event)" in html
    assert "function resizePanelFromKeyboard(event)" in html
    assert "handle.addEventListener('pointerdown', beginPanelResize);" in html
    assert "handle.addEventListener('keydown', resizePanelFromKeyboard);" in html
    assert "document.addEventListener('pointermove', resizePanel);" in html
    assert "document.addEventListener('pointerup', finishResize);" in html
    assert "setPanelHeight(body, startHeight + moveEvent.clientY - startY);" in html
    for key in ["ArrowDown", "ArrowUp", "PageDown", "PageUp", "Home", "End"]:
        assert key in html


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
