from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from voice_assistant.app import RuntimeBundle, create_app
from voice_assistant.constants import EventType, SoundEvent
from voice_assistant.tts_voices import KOKORO_VOICES, phrase_output_filename, sanitize_tts_sound_phrase


SECTION_START_PATTERN = re.compile(r'<section\s+(?P<attrs>[^>]*data-admin-section="(?P<key>[^"]+)"[^>]*)>', re.S)
ATTR_PATTERN = re.compile(r'([A-Za-z_:][-A-Za-z0-9_:.]*)(?:="([^"]*)")?')
KNOWN_ADMIN_SECTION_TITLES = {"Status", "Configuration", "Text-to-Speech Voices", "Sound Library", "Diagnostics", "Telemetry"}
LAZY_LOADERS = {
    "status": "loadStatus",
    "configuration": "loadConfig",
    "tts-voices": "loadTtsVoices",
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
        "Text-to-Speech Voices": ['id="ttsVoiceSelect"', 'loadTtsVoices()', 'ttsSampleText', 'ttsGeneratedPhrases', 'ttsGeneratedSoundFiles', 'testTtsVoice()', 'applyTtsVoice()', 'services.tts.voice', 'sounds.generated_tts_phrases'],
        "Sound Library": ['id="soundFile"', 'loadSounds()', 'empty string', 'command_thinking', 'playSoundEvent()'],
        "Diagnostics": ['id="testText"', 'llmTtsTest()', 'commandTest()'],
        "Telemetry": ['id="events"', 'loadEvents()', 'Search history'],
    }
    for title, tokens in expected_content.items():
        assert title in panels
        for token in tokens:
            assert token in panels[title]



def test_admin_portal_displays_command_thinking_sound_event_controls(bundle_parts):
    client, _ = make_client(bundle_parts)
    html = client.get("/").text
    sounds_payload = client.get("/api/sounds").json()

    assert "Command thinking" in html
    assert 'value="command_thinking"' in html
    assert "function playSoundEvent()" in html
    assert SoundEvent.COMMAND_THINKING.value in sounds_payload["event_files"]

def test_admin_portal_runtime_content_is_not_loaded_until_panel_expands(bundle_parts):
    client, _ = make_client(bundle_parts)
    html = client.get("/").text
    script = _script_block(html)
    initializer = script.split("function initializeAdminPortal(){", 1)[1].split("}\nif(document.readyState", 1)[0]
    panels = {str(panel["key"]): panel for panel in _admin_panels(html)}

    assert "loadStatus(); loadConfig(); loadEvents(); loadSounds();" not in script
    assert "loadTtsVoices();" not in initializer
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



def test_config_export_import_preserves_command_thinking_sound_event(bundle_parts):
    client, _ = make_client(bundle_parts)
    saved = client.get("/api/config").json()["saved"]
    saved["sounds"]["event_files"][SoundEvent.COMMAND_THINKING.value] = "command-thinking-custom.wav"
    saved["sounds"]["event_files"][SoundEvent.THINKING.value] = "thinking-custom.wav"

    applied = client.post("/api/config/apply", json={"config": saved})

    assert applied.status_code == 200
    assert applied.json()["saved"]["sounds"]["event_files"][SoundEvent.COMMAND_THINKING.value] == "command-thinking-custom.wav"
    assert applied.json()["saved"]["sounds"]["event_files"][SoundEvent.THINKING.value] == "thinking-custom.wav"
    exported = client.get("/api/config/export").json()
    assert exported["sounds"]["event_files"][SoundEvent.COMMAND_THINKING.value] == "command-thinking-custom.wav"
    imported = client.post("/api/config/import", json=exported)
    assert imported.status_code == 200
    assert imported.json()["draft"]["sounds"]["event_files"][SoundEvent.COMMAND_THINKING.value] == "command-thinking-custom.wav"



def test_config_draft_saves_command_thinking_sound_event(bundle_parts):
    client, _ = make_client(bundle_parts)
    saved = client.get("/api/config").json()["saved"]
    saved["sounds"]["event_files"][SoundEvent.COMMAND_THINKING.value] = "command-thinking-custom.wav"

    response = client.post("/api/config/draft", json=saved)

    assert response.status_code == 200
    assert response.json()["draft"]["sounds"]["event_files"][SoundEvent.COMMAND_THINKING.value] == "command-thinking-custom.wav"


def test_config_draft_validation_errors_return_400_details(bundle_parts):
    client, _ = make_client(bundle_parts)
    saved = client.get("/api/config").json()["saved"]
    saved["sounds"]["event_files"]["command_ thinking"] = "command-thinking-custom.wav"

    response = client.post("/api/config/draft", json=saved)

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["message"] == "Configuration validation failed."
    assert any("command_thinking" in error["msg"] for error in detail["errors"])


def test_config_apply_validation_errors_return_400_details(bundle_parts):
    client, _ = make_client(bundle_parts)
    saved = client.get("/api/config").json()["saved"]
    saved["sounds"]["event_files"][SoundEvent.COMMAND_THINKING.value] = None

    response = client.post("/api/config/apply", json={"config": saved})

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["message"] == "Configuration validation failed."
    assert any(error["loc"][-1] == SoundEvent.COMMAND_THINKING.value for error in detail["errors"])

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
    assert SoundEvent.COMMAND_THINKING.value in sounds_payload["event_files"]

    saved = client.get("/api/config").json()["saved"]
    saved["sounds"]["event_files"][SoundEvent.COMMAND_THINKING.value] = "custom.wav"
    saved["sounds"]["event_files"][SoundEvent.THINKING.value] = "thinking.wav"
    assert client.post("/api/config/apply", json={"config": saved}).status_code == 200
    assert client.post(f"/api/sound-events/{SoundEvent.COMMAND_THINKING.value}/play").status_code == 200
    assert ("play_sound_event", str(SoundEvent.COMMAND_THINKING)) in audio.calls

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


def test_tts_voices_section_renders_as_collapsible_admin_panel(bundle_parts):
    client, _ = make_client(bundle_parts)
    html = client.get("/").text
    panel = next(panel for panel in _admin_panels(html) if panel["title"] == "Text-to-Speech Voices")
    fragment = str(panel["fragment"])

    assert panel["key"] == "tts-voices"
    assert 'data-load-on-expand="loadTtsVoices"' in fragment
    assert 'id="ttsConfiguredVoice"' in fragment
    assert 'id="ttsSelectedVoice"' in fragment
    assert 'id="ttsVoiceSelect"' in fragment
    assert 'id="ttsSampleText"' in fragment
    assert "Hello, this is a test of this Kokoro voice." in fragment
    assert 'id="ttsGeneratedSoundDirectory"' in fragment
    assert 'id="ttsGeneratedPhrases"' in fragment
    assert 'id="ttsGeneratedSoundFiles"' in fragment
    assert "sounds.generated_tts_phrases" in fragment
    assert "only these target WAV files" in fragment
    assert 'id="ttsTestButton"' in fragment
    assert 'id="ttsApplyButton"' in fragment
    assert "configured physical speaker output" in fragment
    assert "services.tts.voice" in fragment
    assert "chatterbox" not in fragment.lower()


def test_tts_voice_list_endpoint_is_kokoro_only_and_shows_configured_voice(bundle_parts):
    client, (store, *_rest) = make_client(bundle_parts)

    body = client.get("/api/tts-voices").json()
    ids = [item["id"] for item in body["voices"]]

    assert body["provider"] == "kokoro"
    assert body["configured_voice"] == store.get_saved().services.tts.voice == "af_heart"
    assert body["configured_voice_supported"] is True
    assert ids == list(KOKORO_VOICES)
    assert "af_heart" in ids
    assert "bf_emma" in ids
    assert "ff_siwis" in ids
    assert body["voice_count"] == len(KOKORO_VOICES)
    assert "chatterbox" not in str(body).lower()
    assert body["generated_tts_sound_directory"] == store.get_saved().sounds.library_dir
    assert body["generated_tts_sound_files"] == [phrase_output_filename(phrase) for phrase in body["generated_tts_phrases"]]


def test_tts_voice_ui_wires_selection_sample_test_and_apply_controls(bundle_parts):
    client, _ = make_client(bundle_parts)
    html = client.get("/").text
    script = _script_block(html)

    assert "function renderTtsVoiceOptions(data)" in script
    assert "function updateTtsVoiceSelection()" in script
    assert "readyForTest = Boolean(selected && ttsSampleText() && !ttsVoiceState.busy)" in script
    assert "readyForApply = Boolean(selected && ttsGeneratedPhrases().length > 0 && !ttsVoiceState.busy)" in script
    assert "function ttsGeneratedPhrases()" in script
    assert r"split(/\r?\n/)" in script
    assert r"generated_tts_phrases.join('\n')" in script
    assert "generated_tts_phrases.join('" + "\n" + "')" not in script
    assert "function renderTtsGeneratedSoundPreview()" in script
    assert "function ttsPhraseOutputFilename(phrase)" in script
    assert "async function testTtsVoice()" in script
    assert "/api/tts-voices/test" in script
    assert "voice:selectedTtsVoice(), text:ttsSampleText()" in script
    assert "async function applyTtsVoice()" in script
    assert "/api/tts-voices/apply" in script
    assert "body:JSON.stringify({voice:selectedTtsVoice(), phrases})" in script
    assert "setTtsVoiceBusy(true" in script


def test_tts_voice_sample_uses_selected_voice_physical_playback_and_does_not_persist(bundle_parts):
    client, (store, telemetry, runtime, audio, stt, llm, tts) = make_client(bundle_parts)
    voices_seen = []

    def recording_tts_factory(cfg):
        voices_seen.append(cfg.services.tts.voice)
        return tts

    runtime.tts_factory = recording_tts_factory
    original_saved_voice = store.get_saved().services.tts.voice

    response = client.post("/api/tts-voices/test", json={"voice": "bf_emma", "text": "Sample voice text."})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["voice"] == "bf_emma"
    assert body["persisted"] is False
    assert body["played_through"] == "assistant_physical_output"
    assert voices_seen == ["bf_emma"]
    assert tts.inputs == ["Sample voice text."]
    assert ("play_file", body["tts_path"]) in audio.calls
    assert ("play_file_require_playback", body["tts_path"]) in audio.calls
    assert store.get_saved().services.tts.voice == original_saved_voice
    assert store.get_active().services.tts.voice == original_saved_voice


def test_tts_voice_sample_rejects_invalid_voice_without_exposing_secrets(bundle_parts):
    client, (store, *_rest) = make_client(bundle_parts)

    response = client.post("/api/tts-voices/test", json={"voice": "cb_fake", "text": "hello"})

    assert response.status_code == 400
    assert "Unsupported Kokoro voice" in response.text
    assert "test-tts" not in response.text
    assert store.get_saved().services.tts.voice == "af_heart"


def test_tts_voice_sample_reports_tts_and_playback_failures(bundle_parts):
    client, (store, telemetry, runtime, audio, stt, llm, tts) = make_client(bundle_parts)
    tts.exc = RuntimeError("router unavailable")

    response = client.post("/api/tts-voices/test", json={"voice": "bf_emma", "text": "hello"})

    assert response.status_code == 502
    assert "TTS voice sample generation or playback failed" in response.text
    assert "test-tts" not in response.text

    tts.exc = None

    async def fail_playback(cfg, path, *, cancel_event=None, require_playback=False):
        raise RuntimeError("speaker unavailable")

    audio.play_file = fail_playback
    playback_response = client.post("/api/tts-voices/test", json={"voice": "bf_emma", "text": "hello"})

    assert playback_response.status_code == 502
    assert "speaker unavailable" in playback_response.text
    assert store.get_saved().services.tts.voice == "af_heart"


def test_tts_voice_apply_persists_config_regenerates_sounds_and_keeps_uploads(bundle_parts):
    client, (store, telemetry, runtime, audio, stt, llm, tts) = make_client(bundle_parts)
    sound_dir = Path(store.get_active().sounds.library_dir)
    uploaded = sound_dir / "uploaded_custom.wav"
    uploaded.write_bytes(b"custom user upload")
    voices_seen = []

    def recording_tts_factory(cfg):
        voices_seen.append(cfg.services.tts.voice)
        return tts

    runtime.tts_factory = recording_tts_factory
    phrases = list(store.get_saved().sounds.generated_tts_phrases)
    expected_files = {phrase_output_filename(phrase) for phrase in phrases}

    response = client.post("/api/tts-voices/apply", json={"voice": "bf_emma"})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["voice"] == "bf_emma"
    assert body["configuration_path"] == "services.tts.voice"
    assert body["configuration_result"]["saved"]["services"]["tts"]["voice"] == "bf_emma"
    assert body["configuration_result"]["active"]["services"]["tts"]["voice"] == "bf_emma"
    assert "services.tts.voice" in body["configuration_result"]["applied_runtime_paths"]
    assert "services.tts.voice" not in body["configuration_result"]["pending_restart_paths"]
    assert store.get_saved().services.tts.voice == "bf_emma"
    assert store.get_active().services.tts.voice == "bf_emma"
    assert client.get("/api/config").json()["saved"]["services"]["tts"]["voice"] == "bf_emma"
    assert client.get("/api/config/export").json()["services"]["tts"]["voice"] == "bf_emma"
    assert client.post("/api/config/import", json=client.get("/api/config/export").json()).json()["draft"]["services"]["tts"]["voice"] == "bf_emma"
    assert tts.inputs == phrases
    assert voices_seen == ["bf_emma"] * len(phrases)
    regenerated = {item["filename"] for item in body["regeneration"]["generated_files"]}
    assert regenerated == expected_files
    for filename in expected_files:
        assert (sound_dir / filename).exists()
    assert uploaded.exists()
    assert uploaded.read_bytes() == b"custom user upload"




def test_tts_voice_apply_persists_edited_phrase_list_and_regenerates_only_listed_sounds(bundle_parts):
    client, (store, telemetry, runtime, audio, stt, llm, tts) = make_client(bundle_parts)
    sound_dir = Path(store.get_active().sounds.library_dir)
    existing_default = sound_dir / "failure.wav"
    existing_default.write_bytes(b"do not overwrite when omitted")
    uploaded = sound_dir / "uploaded_custom.wav"
    uploaded.write_bytes(b"custom user upload")

    runtime.tts_factory = lambda _cfg: tts
    phrases = ["wake ack", "admin ready", "extra prompt"]
    expected_files = {phrase_output_filename(phrase) for phrase in phrases}

    response = client.post("/api/tts-voices/apply", json={"voice": "bf_emma", "phrases": phrases})

    assert response.status_code == 200
    body = response.json()
    assert body["generated_tts_phrases"] == phrases
    assert body["configuration_result"]["saved"]["sounds"]["generated_tts_phrases"] == phrases
    assert store.get_saved().sounds.generated_tts_phrases == phrases
    assert store.get_active().sounds.generated_tts_phrases == phrases
    assert tts.inputs == phrases
    assert {item["filename"] for item in body["regeneration"]["generated_files"]} == expected_files
    for filename in expected_files:
        assert (sound_dir / filename).exists()
    assert existing_default.read_bytes() == b"do not overwrite when omitted"
    assert uploaded.read_bytes() == b"custom user upload"


def test_tts_voice_apply_rejects_empty_or_duplicate_phrase_targets_without_config_update(bundle_parts):
    client, (store, *_rest) = make_client(bundle_parts)
    original_saved = store.get_saved()

    empty_response = client.post("/api/tts-voices/apply", json={"voice": "bf_emma", "phrases": ["", " "]})

    assert empty_response.status_code == 400
    assert "Invalid generated TTS sound phrase list" in empty_response.text
    assert store.get_saved().services.tts.voice == original_saved.services.tts.voice
    assert store.get_saved().sounds.generated_tts_phrases == original_saved.sounds.generated_tts_phrases

    duplicate_response = client.post("/api/tts-voices/apply", json={"voice": "bf_emma", "phrases": ["Wake ack", "wake ack!"]})

    assert duplicate_response.status_code == 400
    assert "both target" in duplicate_response.text
    assert store.get_saved().services.tts.voice == original_saved.services.tts.voice
    assert store.get_saved().sounds.generated_tts_phrases == original_saved.sounds.generated_tts_phrases


def test_tts_voice_apply_reports_regeneration_failure_without_config_update(bundle_parts):
    client, (store, telemetry, runtime, audio, stt, llm, tts) = make_client(bundle_parts)
    original_saved = store.get_saved()
    tts.exc = RuntimeError("tts generation failed")

    response = client.post("/api/tts-voices/apply", json={"voice": "bf_emma"})

    assert response.status_code == 502
    body = response.json()["detail"]
    assert body["configuration_updated"] is False
    assert body["configuration_result"] is None
    assert "Configuration is not updated unless generated sound regeneration succeeds" in body["message"]
    assert store.get_saved().services.tts.voice == original_saved.services.tts.voice
    assert store.get_active().services.tts.voice == original_saved.services.tts.voice
    assert "test-tts" not in response.text
