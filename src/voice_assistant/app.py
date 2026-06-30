from __future__ import annotations

import asyncio
import json
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, ValidationError

from .assistant import AssistantRuntime
from .commands import CommandRegistry
from .config import AssistantConfig, ConfigStore, load_config_store_from_env
from .constants import ArtifactKind, EventType, SoundEvent
from .health import HealthChecker
from .maintenance import MaintenanceController
from .telemetry import TelemetryFilters, TelemetryStore
from .tts_voices import (
    KOKORO_VOICE_SET,
    config_with_tts_voice,
    kokoro_voice_options,
    normalize_generated_tts_phrases,
    phrase_output_filename,
    regenerate_generated_tts_sounds,
    validate_kokoro_voice,
)


class ApplyRequest(BaseModel):
    config: dict[str, Any] | None = None


class ConfirmRequest(BaseModel):
    confirm: bool = False


class WakeTestRequest(BaseModel):
    confidence: float = 1.0


class ProductionWakeMigrationRequest(BaseModel):
    confirm: bool = False


class LlmTtsTestRequest(BaseModel):
    text: str = Field(..., min_length=1)


class TtsVoiceTestRequest(BaseModel):
    voice: str = Field(..., min_length=1)
    text: str = Field(..., min_length=1, max_length=2000)


class TtsVoiceApplyRequest(BaseModel):
    voice: str = Field(..., min_length=1)
    phrases: list[str] | None = Field(default=None, min_length=1, max_length=100)


class MicrophoneTestRequest(BaseModel):
    duration_seconds: float = Field(5.0, ge=1.0, le=30.0)


class CommandRecognitionTestRequest(BaseModel):
    text: str = Field(..., min_length=1)


class RuntimeBundle:
    def __init__(self, config_store: ConfigStore, telemetry: TelemetryStore, runtime: AssistantRuntime):
        self.config_store = config_store
        self.telemetry = telemetry
        self.runtime = runtime
        self.maintenance = MaintenanceController(telemetry)
        self.tts_voice_operation_lock = asyncio.Lock()


def _build_bundle(config_store: ConfigStore | None = None, runtime: AssistantRuntime | None = None) -> RuntimeBundle:
    config_store = config_store or load_config_store_from_env()
    cfg = config_store.get_active()
    telemetry = TelemetryStore(cfg.storage.telemetry_db_path, cfg.storage.artifacts_dir)
    runtime = runtime or AssistantRuntime(config_store, telemetry)
    return RuntimeBundle(config_store, telemetry, runtime)


def _safe_filename(name: str) -> str:
    safe = Path(name).name.strip().replace(" ", "_")
    if not safe:
        safe = f"sound-{uuid.uuid4()}.wav"
    return safe


def _sound_dir(cfg: AssistantConfig) -> Path:
    path = Path(cfg.sounds.library_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _raise_config_validation_error(exc: ValidationError) -> None:
    raise HTTPException(
        status_code=400,
        detail={
            "message": "Configuration validation failed.",
            "errors": exc.errors(include_url=False, include_input=False),
        },
    ) from exc


def _validate_voice_or_400(voice: str) -> str:
    try:
        return validate_kokoro_voice(voice)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"message": str(exc)}) from exc


def _public_error(exc: BaseException) -> str:
    return str(exc) or exc.__class__.__name__


INDEX_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Local Voice Assistant Admin</title>
  <style>
    [hidden] { display: none !important; }
    body { font-family: system-ui, sans-serif; margin: 2rem; line-height: 1.45; }
    section.admin-panel { background: #fff; border: 1px solid #ddd; margin: 1rem 0; border-radius: 0.5rem; overflow: hidden; padding: 0; }
    section.admin-panel h2 { margin: 0; }
    .admin-panel-toggle { align-items: center; background: #f7f7f7; border: 0; cursor: pointer; display: flex; font-size: 1.25rem; font-weight: 700; gap: 0.75rem; justify-content: space-between; margin: 0; padding: 1rem; text-align: left; width: 100%; }
    .admin-panel-toggle:focus { outline: 2px solid #333; outline-offset: -2px; }
    .admin-panel-toggle:hover { background: #f0f0f0; }
    .admin-panel-marker { align-items: center; border: 1px solid #bbb; border-radius: 999px; display: inline-flex; flex: 0 0 auto; height: 1.5rem; justify-content: center; width: 1.5rem; }
    .admin-panel-title { flex: 1 1 auto; }
    .admin-panel-state { font-size: 0.9rem; font-weight: 600; margin-left: 1rem; white-space: nowrap; }
    .admin-panel-body { border-top: 1px solid #ddd; box-sizing: border-box; display: flex; flex-direction: column; height: 20rem; min-height: 12rem; max-height: min(75vh, 45rem); overflow: hidden; padding: 0; }
    section.admin-panel.is-collapsed > .admin-panel-body,
    section.admin-panel > .admin-panel-body[hidden] { border: 0 !important; display: none !important; height: 0 !important; max-height: 0 !important; min-height: 0 !important; overflow: hidden !important; padding: 0 !important; visibility: hidden !important; }
    section.admin-panel.is-collapsed [data-runtime-output] { margin: 0 !important; max-height: 0 !important; overflow: hidden !important; padding: 0 !important; }
    .admin-panel-body-content { flex: 1 1 auto; min-height: 0; overflow: auto; padding: 1rem; }
    .admin-panel-resize-handle { align-items: center; background: #fafafa; border-top: 1px solid #ddd; box-sizing: border-box; color: #555; cursor: ns-resize; display: flex; flex: 0 0 auto; font-size: 0.8rem; justify-content: center; letter-spacing: 0.02em; padding: 0.35rem 1rem; user-select: none; width: 100%; }
    .admin-panel-resize-handle:focus { outline: 2px solid #333; outline-offset: -2px; }
    .admin-panel-resize-handle::before { content: "||"; font-size: 1rem; margin-right: 0.4rem; }
    body.resizing-admin-panel, body.resizing-admin-panel * { cursor: ns-resize !important; user-select: none; }
    textarea { width: 100%; min-height: 12rem; font-family: ui-monospace, monospace; }
    input, button, select { margin: 0.2rem; padding: 0.35rem; }
    pre { background: #f6f6f6; padding: 1rem; overflow: auto; }
    .warn { color: #8a4b00; }
  </style>
</head>
<body>
  <h1>Local Voice Assistant Admin</h1>
  <p class="warn">V1 has no authentication by requirement. Keep this service on the trusted local network only.</p>
  <section class="admin-panel is-collapsed" data-admin-section="status">
    <h2>
      <button id="toggle-status" class="admin-panel-toggle" type="button" aria-expanded="false" aria-controls="panel-status" onclick="togglePanel(this)">
        <span class="admin-panel-marker" data-panel-marker aria-hidden="true">+</span>
        <span class="admin-panel-title">Status</span>
        <span class="admin-panel-state" data-panel-state>Collapsed - click to expand</span>
      </button>
    </h2>
    <div id="panel-status" class="admin-panel-body" data-resizable-panel data-load-on-expand="loadStatus" role="region" aria-labelledby="toggle-status" aria-hidden="true" hidden>
      <div id="panel-status-content" class="admin-panel-body-content" data-panel-content>
        <button onclick="loadStatus()">Refresh status</button>
        <button onclick="simulateWake()">Simulate wake (admin-only)</button>
        <button onclick="loadHealth()">Refresh health</button>
        <button onclick="loadWakeDebug()">Wake debug</button>
        <button onclick="migrateProductionWake()">Migrate saved config to production wake</button>
        <pre id="status" data-runtime-output></pre>
        <pre id="health" data-runtime-output></pre>
      </div>
      <div class="admin-panel-resize-handle" data-resize-handle role="separator" aria-orientation="horizontal" aria-controls="panel-status-content" aria-label="Resize Status section height" aria-valuemin="192" aria-valuemax="720" aria-valuenow="320" tabindex="0">Drag or use arrow keys to resize</div>
    </div>
  </section>
  <section class="admin-panel is-collapsed" data-admin-section="configuration">
    <h2>
      <button id="toggle-configuration" class="admin-panel-toggle" type="button" aria-expanded="false" aria-controls="panel-configuration" onclick="togglePanel(this)">
        <span class="admin-panel-marker" data-panel-marker aria-hidden="true">+</span>
        <span class="admin-panel-title">Configuration</span>
        <span class="admin-panel-state" data-panel-state>Collapsed - click to expand</span>
      </button>
    </h2>
    <div id="panel-configuration" class="admin-panel-body" data-resizable-panel data-load-on-expand="loadConfig" role="region" aria-labelledby="toggle-configuration" aria-hidden="true" hidden>
      <div id="panel-configuration-content" class="admin-panel-body-content" data-panel-content>
        <button onclick="loadConfig()">Load config</button>
        <button onclick="saveDraft()">Save draft</button>
        <button onclick="applyDraft()">Apply saved draft</button>
        <a href="/api/config/export" download="voice-assistant-config.json">Export saved config</a>
        <p>Edits are applied as a group. STT/LLM/TTS connection changes are saved as pending-restart values.</p>
        <textarea id="config"></textarea>
        <pre id="configResult" data-runtime-output></pre>
      </div>
      <div class="admin-panel-resize-handle" data-resize-handle role="separator" aria-orientation="horizontal" aria-controls="panel-configuration-content" aria-label="Resize Configuration section height" aria-valuemin="192" aria-valuemax="720" aria-valuenow="320" tabindex="0">Drag or use arrow keys to resize</div>
    </div>
  </section>
  <section class="admin-panel is-collapsed" data-admin-section="tts-voices">
    <h2>
      <button id="toggle-tts-voices" class="admin-panel-toggle" type="button" aria-expanded="false" aria-controls="panel-tts-voices" onclick="togglePanel(this)">
        <span class="admin-panel-marker" data-panel-marker aria-hidden="true">+</span>
        <span class="admin-panel-title">Text-to-Speech Voices</span>
        <span class="admin-panel-state" data-panel-state>Collapsed - click to expand</span>
      </button>
    </h2>
    <div id="panel-tts-voices" class="admin-panel-body" data-resizable-panel data-load-on-expand="loadTtsVoices" role="region" aria-labelledby="toggle-tts-voices" aria-hidden="true" hidden>
      <div id="panel-tts-voices-content" class="admin-panel-body-content" data-panel-content>
        <p>Select a Kokoro voice, test it through the assistant machine's configured physical speaker output, then commit it to <code>services.tts.voice</code>.</p>
        <p class="warn">Committing a voice also regenerates and overwrites exactly the generated sound phrases listed below. It does not delete unrelated uploaded sounds.</p>
        <p>Configured voice: <strong id="ttsConfiguredVoice">not loaded</strong></p>
        <p>Selected test candidate: <strong id="ttsSelectedVoice">not loaded</strong> <span id="ttsCandidateNotice" class="warn"></span></p>
        <button onclick="loadTtsVoices()">Refresh Kokoro voice list</button>
        <label for="ttsVoiceSelect">Kokoro voice</label>
        <select id="ttsVoiceSelect" onchange="updateTtsVoiceSelection()"></select>
        <p><label for="ttsSampleText">Sample text</label></p>
        <textarea id="ttsSampleText" oninput="updateTtsVoiceSelection()">Hello, this is a test of this Kokoro voice.</textarea>
        <p>Generated sound directory: <code id="ttsGeneratedSoundDirectory">not loaded</code></p>
        <p><label for="ttsGeneratedPhrases">Generated sound phrases to overwrite, one per line</label></p>
        <textarea id="ttsGeneratedPhrases" oninput="renderTtsGeneratedSoundPreview()" placeholder="wake ack&#10;prompt accepted&#10;command accepted"></textarea>
        <p>Edit this list before applying. The apply action saves this phrase list to <code>sounds.generated_tts_phrases</code> and regenerates only these target WAV files using the selected Kokoro voice.</p>
        <p>Target WAV files preview:</p>
        <pre id="ttsGeneratedSoundFiles" data-runtime-output></pre>
        <button id="ttsTestButton" onclick="testTtsVoice()" disabled>Test selected voice through speakerphone</button>
        <button id="ttsApplyButton" onclick="applyTtsVoice()" disabled>Commit selected voice and regenerate listed sounds</button>
        <pre id="ttsVoicesResult" data-runtime-output></pre>
      </div>
      <div class="admin-panel-resize-handle" data-resize-handle role="separator" aria-orientation="horizontal" aria-controls="panel-tts-voices-content" aria-label="Resize Text-to-Speech Voices section height" aria-valuemin="192" aria-valuemax="720" aria-valuenow="320" tabindex="0">Drag or use arrow keys to resize</div>
    </div>
  </section>
  <section class="admin-panel is-collapsed" data-admin-section="sound-library">
    <h2>
      <button id="toggle-sound-library" class="admin-panel-toggle" type="button" aria-expanded="false" aria-controls="panel-sound-library" onclick="togglePanel(this)">
        <span class="admin-panel-marker" data-panel-marker aria-hidden="true">+</span>
        <span class="admin-panel-title">Sound Library</span>
        <span class="admin-panel-state" data-panel-state>Collapsed - click to expand</span>
      </button>
    </h2>
    <div id="panel-sound-library" class="admin-panel-body" data-resizable-panel data-load-on-expand="loadSounds" role="region" aria-labelledby="toggle-sound-library" aria-hidden="true" hidden>
      <div id="panel-sound-library-content" class="admin-panel-body-content" data-panel-content>
        <p>Upload WAV files, preferably simple uncompressed PCM WAV. V1 intentionally performs only light filename checks; use playback tests to verify audio. Set a sound event file to an empty string to intentionally disable sound for that event.</p>
        <p>Assign uploaded files in Configuration under <code>sounds.event_files</code>. Command thinking uses <code>command_thinking</code> for the local command interpretation phase.</p>
        <input id="soundFile" type="file" /> <button onclick="uploadSound()">Upload</button>
        <button onclick="loadSounds()">List sounds</button>
        <input id="soundEventName" value="command_thinking" /> <button onclick="playSoundEvent()">Test configured sound event</button>
        <pre id="sounds" data-runtime-output></pre>
      </div>
      <div class="admin-panel-resize-handle" data-resize-handle role="separator" aria-orientation="horizontal" aria-controls="panel-sound-library-content" aria-label="Resize Sound Library section height" aria-valuemin="192" aria-valuemax="720" aria-valuenow="320" tabindex="0">Drag or use arrow keys to resize</div>
    </div>
  </section>
  <section class="admin-panel is-collapsed" data-admin-section="diagnostics">
    <h2>
      <button id="toggle-diagnostics" class="admin-panel-toggle" type="button" aria-expanded="false" aria-controls="panel-diagnostics" onclick="togglePanel(this)">
        <span class="admin-panel-marker" data-panel-marker aria-hidden="true">+</span>
        <span class="admin-panel-title">Diagnostics</span>
        <span class="admin-panel-state" data-panel-state>Collapsed - click to expand</span>
      </button>
    </h2>
    <div id="panel-diagnostics" class="admin-panel-body" data-resizable-panel role="region" aria-labelledby="toggle-diagnostics" aria-hidden="true" hidden>
      <div id="panel-diagnostics-content" class="admin-panel-body-content" data-panel-content>
        <input id="testText" value="Say this through the assistant path." size="60" />
        <button onclick="llmTtsTest()">Typed LLM/TTS test through speakerphone</button>
        <button onclick="micTest()">5s microphone test</button>
        <input id="commandText" value="stop" /> <button onclick="commandTest()">Local command test</button>
        <pre id="tests" data-runtime-output></pre>
      </div>
      <div class="admin-panel-resize-handle" data-resize-handle role="separator" aria-orientation="horizontal" aria-controls="panel-diagnostics-content" aria-label="Resize Diagnostics section height" aria-valuemin="192" aria-valuemax="720" aria-valuenow="320" tabindex="0">Drag or use arrow keys to resize</div>
    </div>
  </section>
  <section class="admin-panel is-collapsed" data-admin-section="telemetry">
    <h2>
      <button id="toggle-telemetry" class="admin-panel-toggle" type="button" aria-expanded="false" aria-controls="panel-telemetry" onclick="togglePanel(this)">
        <span class="admin-panel-marker" data-panel-marker aria-hidden="true">+</span>
        <span class="admin-panel-title">Telemetry</span>
        <span class="admin-panel-state" data-panel-state>Collapsed - click to expand</span>
      </button>
    </h2>
    <div id="panel-telemetry" class="admin-panel-body" data-resizable-panel data-load-on-expand="loadEvents" role="region" aria-labelledby="toggle-telemetry" aria-hidden="true" hidden>
      <div id="panel-telemetry-content" class="admin-panel-body-content" data-panel-content>
        <input id="search" placeholder="search" /> <button onclick="loadEvents()">Search history</button>
        <pre id="events" data-runtime-output></pre>
      </div>
      <div class="admin-panel-resize-handle" data-resize-handle role="separator" aria-orientation="horizontal" aria-controls="panel-telemetry-content" aria-label="Resize Telemetry section height" aria-valuemin="192" aria-valuemax="720" aria-valuenow="320" tabindex="0">Drag or use arrow keys to resize</div>
    </div>
  </section>
<script>
const PANEL_MIN_HEIGHT = 192;
const PANEL_DEFAULT_HEIGHT = 320;
const PANEL_MAX_HEIGHT = 720;
function panelMaxHeight(){
  const viewportHeight = window.innerHeight || PANEL_MAX_HEIGHT;
  return Math.max(PANEL_MIN_HEIGHT, Math.min(PANEL_MAX_HEIGHT, Math.floor(viewportHeight * 0.75)));
}
function clampPanelHeight(height){ return Math.min(panelMaxHeight(), Math.max(PANEL_MIN_HEIGHT, Math.round(height || PANEL_DEFAULT_HEIGHT))); }
function currentPanelHeight(body){ return body.getBoundingClientRect().height || parseInt(body.style.height, 10) || PANEL_DEFAULT_HEIGHT; }
function updateResizeHandleValue(body){
  const handle = body ? body.querySelector('[data-resize-handle]') : null;
  if(!handle){ return; }
  const currentHeight = currentPanelHeight(body);
  handle.setAttribute('aria-valuemin', String(PANEL_MIN_HEIGHT));
  handle.setAttribute('aria-valuemax', String(panelMaxHeight()));
  handle.setAttribute('aria-valuenow', String(clampPanelHeight(currentHeight)));
}
function setPanelHeight(body, height){
  if(!body){ return; }
  body.style.height = clampPanelHeight(height) + 'px';
  updateResizeHandleValue(body);
}
function beginPanelResize(event){
  if(event.button !== undefined && event.button !== 0){ return; }
  const handle = event.currentTarget;
  const body = handle.closest('[data-resizable-panel]');
  if(!body || body.hidden){ return; }
  event.preventDefault();
  const startY = event.clientY;
  const startHeight = currentPanelHeight(body);
  handle.setAttribute('aria-grabbed', 'true');
  document.body.classList.add('resizing-admin-panel');
  if(handle.setPointerCapture && event.pointerId !== undefined){ handle.setPointerCapture(event.pointerId); }
  function finishResize(finalEvent){
    document.removeEventListener('pointermove', resizePanel);
    document.removeEventListener('pointerup', finishResize);
    document.removeEventListener('pointercancel', finishResize);
    handle.setAttribute('aria-grabbed', 'false');
    document.body.classList.remove('resizing-admin-panel');
    if(handle.releasePointerCapture && finalEvent && finalEvent.pointerId !== undefined){
      try { handle.releasePointerCapture(finalEvent.pointerId); } catch(_err) {}
    }
  }
  function resizePanel(moveEvent){
    moveEvent.preventDefault();
    setPanelHeight(body, startHeight + moveEvent.clientY - startY);
  }
  document.addEventListener('pointermove', resizePanel);
  document.addEventListener('pointerup', finishResize);
  document.addEventListener('pointercancel', finishResize);
}
function resizePanelFromKeyboard(event){
  const body = event.currentTarget.closest('[data-resizable-panel]');
  if(!body || body.hidden){ return; }
  const current = currentPanelHeight(body);
  const step = event.shiftKey ? 80 : 32;
  if(event.key === 'ArrowDown'){
    event.preventDefault();
    setPanelHeight(body, current + step);
  } else if(event.key === 'ArrowUp'){
    event.preventDefault();
    setPanelHeight(body, current - step);
  } else if(event.key === 'PageDown'){
    event.preventDefault();
    setPanelHeight(body, current + 120);
  } else if(event.key === 'PageUp'){
    event.preventDefault();
    setPanelHeight(body, current - 120);
  } else if(event.key === 'Home'){
    event.preventDefault();
    setPanelHeight(body, PANEL_MIN_HEIGHT);
  } else if(event.key === 'End'){
    event.preventDefault();
    setPanelHeight(body, panelMaxHeight());
  }
}
function initializeResizablePanels(){
  document.querySelectorAll('[data-resize-handle]').forEach((handle) => {
    if(handle.dataset.resizeBound === 'true'){ return; }
    handle.dataset.resizeBound = 'true';
    handle.setAttribute('aria-grabbed', 'false');
    handle.addEventListener('pointerdown', beginPanelResize);
    handle.addEventListener('keydown', resizePanelFromKeyboard);
    updateResizeHandleValue(handle.closest('[data-resizable-panel]'));
  });
  window.addEventListener('resize', () => {
    document.querySelectorAll('[data-resizable-panel]').forEach(updateResizeHandleValue);
  });
}
function loadPanelOnFirstExpand(section, body){
  const loaderName = body ? body.dataset.loadOnExpand : '';
  if(!loaderName || body.dataset.loaded === 'true'){ return; }
  const loader = window[loaderName];
  if(typeof loader !== 'function'){ return; }
  body.dataset.loaded = 'true';
  Promise.resolve(loader()).catch((err) => {
    body.dataset.loaded = 'false';
    console.error(err);
  });
}
function setPanelExpanded(button, expanded){
  const body = document.getElementById(button.getAttribute('aria-controls'));
  const section = button.closest('section.admin-panel');
  button.setAttribute('aria-expanded', String(expanded));
  if(body){
    body.hidden = !expanded;
    body.setAttribute('aria-hidden', String(!expanded));
  }
  if(section){
    section.classList.toggle('is-expanded', expanded);
    section.classList.toggle('is-collapsed', !expanded);
  }
  const state = button.querySelector('[data-panel-state]');
  if(state){ state.textContent = expanded ? 'Expanded - click to collapse' : 'Collapsed - click to expand'; }
  const marker = button.querySelector('[data-panel-marker]');
  if(marker){ marker.textContent = expanded ? '-' : '+'; }
  if(expanded && body){
    if(!body.style.height){ body.style.height = PANEL_DEFAULT_HEIGHT + 'px'; }
    updateResizeHandleValue(body);
    loadPanelOnFirstExpand(section, body);
  }
}
function togglePanel(button){
  const nextExpanded = button.getAttribute('aria-expanded') !== 'true';
  setPanelExpanded(button, nextExpanded);
}
function keepCollapsedPanelHidden(target){
  const section = target ? target.closest('section.admin-panel') : null;
  if(!section || !section.classList.contains('is-collapsed')){ return; }
  const body = section.querySelector('[data-resizable-panel]');
  if(body){
    body.hidden = true;
    body.setAttribute('aria-hidden', 'true');
  }
}
function writeText(id, value){
  const target = document.getElementById(id);
  if(!target){ return; }
  target.textContent = value;
  keepCollapsedPanelHidden(target);
}
function writeJson(id, value){ writeText(id, JSON.stringify(value, null, 2)); }
let ttsVoiceState = { configuredVoice: '', voices: [], busy: false, soundDirectory: '' };
function selectedTtsVoice(){
  const select = document.getElementById('ttsVoiceSelect');
  return select ? select.value : '';
}
function ttsSampleText(){
  const sample = document.getElementById('ttsSampleText');
  return sample ? sample.value.trim() : '';
}
function ttsGeneratedPhrases(){
  const phrases = document.getElementById('ttsGeneratedPhrases');
  if(!phrases){ return []; }
  return phrases.value.split(/\\r?\\n/).map((line) => line.trim()).filter(Boolean);
}
function ttsPhraseOutputFilename(phrase){
  let normalized = (phrase || '').trim();
  if(normalized.normalize){ normalized = normalized.normalize('NFKD').replace(/[\u0300-\u036f]/g, ''); }
  normalized = normalized.toLowerCase();
  normalized = normalized.replace(/&/g, ' and ');
  normalized = normalized.replace(/['’`]/g, '');
  normalized = normalized.replace(/[^a-z0-9]+/g, '_').replace(/_+/g, '_').replace(/^_+|_+$/g, '');
  return (normalized || 'sound') + '.wav';
}
function renderTtsGeneratedSoundPreview(){
  const phrases = ttsGeneratedPhrases();
  const files = phrases.map((phrase) => ({phrase, filename: ttsPhraseOutputFilename(phrase)}));
  writeJson('ttsGeneratedSoundFiles', {sound_directory: ttsVoiceState.soundDirectory || 'configured sounds.library_dir', phrase_count: phrases.length, generated_sound_files: files});
  updateTtsVoiceSelection();
}
function setTtsVoiceBusy(busy, message){
  ttsVoiceState.busy = busy;
  const status = document.getElementById('ttsVoicesResult');
  if(message && status){ writeJson('ttsVoicesResult', {status: 'running', message}); }
  updateTtsVoiceSelection();
}
function updateTtsVoiceSelection(){
  const configured = ttsVoiceState.configuredVoice || '';
  const selected = selectedTtsVoice();
  const configuredTarget = document.getElementById('ttsConfiguredVoice');
  const selectedTarget = document.getElementById('ttsSelectedVoice');
  const notice = document.getElementById('ttsCandidateNotice');
  const testButton = document.getElementById('ttsTestButton');
  const applyButton = document.getElementById('ttsApplyButton');
  if(configuredTarget){ configuredTarget.textContent = configured || 'not loaded'; }
  if(selectedTarget){ selectedTarget.textContent = selected || 'not selected'; }
  if(notice){ notice.textContent = configured && selected && configured !== selected ? 'Differs from the configured voice until committed.' : ''; }
  const readyForTest = Boolean(selected && ttsSampleText() && !ttsVoiceState.busy);
  const readyForApply = Boolean(selected && ttsGeneratedPhrases().length > 0 && !ttsVoiceState.busy);
  if(testButton){ testButton.disabled = !readyForTest; }
  if(applyButton){ applyButton.disabled = !readyForApply; }
}
function renderTtsVoiceOptions(data){
  ttsVoiceState.configuredVoice = data.configured_voice || '';
  ttsVoiceState.voices = Array.isArray(data.voices) ? data.voices : [];
  ttsVoiceState.soundDirectory = data.generated_tts_sound_directory || '';
  writeText('ttsGeneratedSoundDirectory', ttsVoiceState.soundDirectory || 'not loaded');
  const phrasesBox = document.getElementById('ttsGeneratedPhrases');
  if(phrasesBox && Array.isArray(data.generated_tts_phrases) && !phrasesBox.value.trim()){ phrasesBox.value = data.generated_tts_phrases.join('\n'); }
  renderTtsGeneratedSoundPreview();
  const select = document.getElementById('ttsVoiceSelect');
  if(!select){ return; }
  select.textContent = '';
  const configuredVoice = ttsVoiceState.configuredVoice;
  let configuredFound = false;
  let lastLanguage = '';
  let currentGroup = null;
  ttsVoiceState.voices.forEach((voice) => {
    const language = voice.language || 'Kokoro';
    if(language !== lastLanguage){
      currentGroup = document.createElement('optgroup');
      currentGroup.label = language;
      select.appendChild(currentGroup);
      lastLanguage = language;
    }
    const option = document.createElement('option');
    option.value = voice.id || voice.voice;
    option.textContent = voice.label || option.value;
    if(option.value === configuredVoice){ configuredFound = true; option.selected = true; }
    if(currentGroup){ currentGroup.appendChild(option); } else { select.appendChild(option); }
  });
  if(configuredVoice && !configuredFound){
    const option = document.createElement('option');
    option.value = configuredVoice;
    option.textContent = configuredVoice + ' (configured but not in Kokoro list)';
    option.disabled = true;
    option.selected = true;
    select.insertBefore(option, select.firstChild);
  }
  updateTtsVoiceSelection();
}
async function j(url, opts={}) { const r = await fetch(url, opts); const t = await r.text(); try { return JSON.parse(t); } catch { return t; } }
async function loadStatus(){ writeJson('status', await j('/api/status')); }
async function loadHealth(){ writeJson('health', await j('/api/health')); }
async function loadWakeDebug(){ writeJson('health', await j('/api/wake/debug')); }
async function migrateProductionWake(){ if(!confirm('Update saved config to the packaged local PocketSphinx production wake engine?')) return; writeJson('configResult', await j('/api/config/migrate-production-wake', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({confirm:true})})); await loadStatus(); await loadConfig(); }
async function simulateWake(){ writeJson('status', await j('/api/test/wake', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({confidence:1})})); }
async function loadConfig(){ const data = await j('/api/config'); document.getElementById('config').value = JSON.stringify(data.saved, null, 2); writeJson('configResult', data); }
async function saveDraft(){ const body = JSON.parse(document.getElementById('config').value); writeJson('configResult', await j('/api/config/draft', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify(body)})); }
async function applyDraft(){ writeJson('configResult', await j('/api/config/apply', {method:'POST', headers:{'content-type':'application/json'}, body:'{}'})); }
async function uploadSound(){ const fd = new FormData(); const f = document.getElementById('soundFile').files[0]; fd.append('file', f); writeJson('sounds', await j('/api/sounds', {method:'POST', body:fd})); }
async function loadSounds(){ writeJson('sounds', await j('/api/sounds')); }
async function playSoundEvent(){ const eventName = encodeURIComponent(document.getElementById('soundEventName').value); writeJson('sounds', await j('/api/sound-events/'+eventName+'/play', {method:'POST'})); }
async function refreshTtsVoiceState(){ const data = await j('/api/tts-voices'); renderTtsVoiceOptions(data); return data; }
async function loadTtsVoices(){ writeJson('ttsVoicesResult', await refreshTtsVoiceState()); }
async function testTtsVoice(){ if(ttsVoiceState.busy){ return; } setTtsVoiceBusy(true, 'Generating and playing the selected voice sample through the configured speakerphone output.'); try { const result = await j('/api/tts-voices/test', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({voice:selectedTtsVoice(), text:ttsSampleText()})}); writeJson('ttsVoicesResult', result); } finally { setTtsVoiceBusy(false); } }
async function applyTtsVoice(){ if(ttsVoiceState.busy){ return; } const phrases = ttsGeneratedPhrases(); if(!phrases.length){ writeJson('ttsVoicesResult', {status:'error', message:'Enter at least one generated sound phrase before applying.'}); return; } if(!confirm('Commit this TTS voice and overwrite exactly '+phrases.length+' generated sound file(s) from the phrase list?')) return; setTtsVoiceBusy(true, 'Committing the selected voice and regenerating the listed generated assistant sounds.'); try { const result = await j('/api/tts-voices/apply', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({voice:selectedTtsVoice(), phrases})}); writeJson('ttsVoicesResult', result); await loadConfig(); await loadSounds(); const refreshed = await refreshTtsVoiceState(); writeJson('ttsVoicesResult', {apply_result: result, refreshed_voice_state: refreshed}); } finally { setTtsVoiceBusy(false); } }
async function llmTtsTest(){ writeJson('tests', await j('/api/test/llm-tts', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({text:document.getElementById('testText').value})})); }
async function micTest(){ writeJson('tests', await j('/api/test/microphone', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({duration_seconds:5})})); }
async function commandTest(){ writeJson('tests', await j('/api/test/command-recognition', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({text:document.getElementById('commandText').value})})); }
async function loadEvents(){ const q = encodeURIComponent(document.getElementById('search').value); writeJson('events', await j('/api/telemetry/events?search='+q)); }
function initializeAdminPortal(){
  initializeResizablePanels();
  document.querySelectorAll('.admin-panel-toggle').forEach((button) => setPanelExpanded(button, false));
}
if(document.readyState === 'loading'){
  document.addEventListener('DOMContentLoaded', initializeAdminPortal);
} else {
  initializeAdminPortal();
}
</script>
</body>
</html>
"""


def create_app(bundle: RuntimeBundle | None = None) -> FastAPI:
    bundle = bundle or _build_bundle()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # In tests, callers may choose not to start the wake loop. Production starts it here.
        if not getattr(app.state, "disable_runtime_start", False):
            await bundle.runtime.start()
        try:
            yield
        finally:
            await bundle.runtime.stop()

    app = FastAPI(title="Local Voice Assistant Admin", version="1.0.0", lifespan=lifespan)
    app.state.bundle = bundle

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse(INDEX_HTML, headers={"Cache-Control": "no-store, max-age=0"})

    @app.get("/api/status")
    async def status() -> dict[str, Any]:
        return bundle.runtime.status()

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        cfg = bundle.config_store.get_active()
        result = await HealthChecker(cfg, wake_runtime_status=bundle.runtime.wake_status()).check_all()
        bundle.telemetry.log_event(EventType.HEALTH, "Health check completed.", component="admin", success=result["ok"], data=result)
        return result

    @app.get("/api/wake/debug")
    async def wake_debug() -> dict[str, Any]:
        wake_events = bundle.telemetry.query_events(event_type=str(EventType.WAKE_DETECTED), limit=20)
        barge_events = bundle.telemetry.query_events(event_type=str(EventType.BARGE_IN), limit=20)
        return {
            "wake_status": bundle.runtime.wake_status(),
            "recent_wake_events": [event.model_dump(mode="json") for event in wake_events],
            "recent_barge_in_events": [event.model_dump(mode="json") for event in barge_events],
            "simulated_admin_endpoint": "/api/test/wake",
            "production_note": "Normal input must come from the configured local wake engine, not the simulated/admin endpoint.",
        }

    @app.get("/api/config")
    async def get_config() -> dict[str, Any]:
        return bundle.config_store.snapshot()

    @app.post("/api/config/draft")
    async def save_config_draft(patch: dict[str, Any]) -> dict[str, Any]:
        try:
            draft = bundle.config_store.save_draft(patch)
        except ValidationError as exc:
            _raise_config_validation_error(exc)
        bundle.telemetry.log_event(EventType.CONFIG, "Configuration draft saved.", component="admin", success=True)
        return {"draft": draft.public_dict()}

    @app.post("/api/config/apply")
    async def apply_config(request: ApplyRequest) -> dict[str, Any]:
        try:
            if request.config is not None:
                result = bundle.config_store.apply_config(request.config)
            else:
                result = bundle.config_store.apply_draft()
        except FileNotFoundError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ValidationError as exc:
            _raise_config_validation_error(exc)
        await bundle.runtime.reload_runtime_components()
        bundle.telemetry.log_event(
            EventType.CONFIG,
            "Configuration changes applied.",
            component="admin",
            success=True,
            data={
                "pending_restart_paths": result.pending_restart_paths,
                "applied_runtime_paths": result.applied_runtime_paths,
            },
        )
        return result.model_dump(mode="json")

    @app.post("/api/config/migrate-production-wake")
    async def migrate_production_wake(request: ProductionWakeMigrationRequest) -> dict[str, Any]:
        if not request.confirm:
            raise HTTPException(status_code=400, detail="Set confirm=true to migrate saved wake config to the packaged production wake engine.")
        result = bundle.config_store.migrate_to_production_wake()
        await bundle.runtime.reload_runtime_components()
        bundle.telemetry.log_event(
            EventType.CONFIG,
            "Saved configuration migrated to the packaged production wake engine.",
            component="admin",
            success=True,
            data={
                "wake_engine": result.saved["wake"]["engine"],
                "external_command": result.saved["wake"].get("external_command"),
                "active_wake_phrase": result.saved["wake"].get("active_wake_phrase"),
                "applied_runtime_paths": result.applied_runtime_paths,
            },
        )
        return {
            **result.model_dump(mode="json"),
            "message": "Saved config now uses the packaged local PocketSphinx external_command wake engine with Rosalina as the active production phrase. Verify /api/status and /api/health, then use voice-only wake.",
        }

    @app.get("/api/config/export")
    async def export_config() -> JSONResponse:
        return JSONResponse(bundle.config_store.get_saved().public_dict())

    @app.post("/api/config/import")
    async def import_config(imported: dict[str, Any]) -> dict[str, Any]:
        try:
            draft = bundle.config_store.import_to_draft(imported)
        except ValidationError as exc:
            _raise_config_validation_error(exc)
        bundle.telemetry.log_event(EventType.CONFIG, "Configuration imported to draft.", component="admin", success=True)
        return {"draft": draft.public_dict(), "message": "Imported configuration saved as draft; use apply to persist it."}

    @app.get("/api/tts-voices")
    async def list_tts_voices() -> dict[str, Any]:
        cfg = bundle.config_store.get_saved()
        try:
            voices = kokoro_voice_options()
        except Exception as exc:  # pragma: no cover - defensive for future dynamic providers
            bundle.telemetry.log_event(
                EventType.ADMIN_TEST,
                "TTS voice list retrieval failed.",
                component="admin",
                success=False,
                error=_public_error(exc),
            )
            raise HTTPException(status_code=502, detail={"message": "TTS voice list retrieval failed.", "error": _public_error(exc)}) from exc
        configured_voice = cfg.services.tts.voice
        generated_phrases = list(cfg.sounds.generated_tts_phrases)
        return {
            "provider": "kokoro",
            "configured_voice": configured_voice,
            "configured_voice_supported": configured_voice in KOKORO_VOICE_SET,
            "voices": voices,
            "voice_count": len(voices),
            "generated_tts_phrases": generated_phrases,
            "generated_tts_sound_directory": cfg.sounds.library_dir,
            "generated_tts_sound_files": [phrase_output_filename(phrase) for phrase in generated_phrases],
            "sample_default": "Hello, this is a test of this Kokoro voice.",
            "playback_path": "assistant_physical_output",
            "configuration_path": "services.tts.voice",
            "notes": [
                "Voice tests use the selected voice without persisting configuration.",
                "Committing a voice persists services.tts.voice and regenerates generated assistant sound files.",
            ],
        }

    @app.post("/api/tts-voices/test")
    async def test_tts_voice(request: TtsVoiceTestRequest) -> dict[str, Any]:
        voice = _validate_voice_or_400(request.voice)
        if bundle.tts_voice_operation_lock.locked():
            raise HTTPException(status_code=409, detail={"message": "Another TTS voice operation is already running."})
        async with bundle.tts_voice_operation_lock:
            cfg = bundle.config_store.get_active()
            test_cfg = config_with_tts_voice(cfg, voice)
            interaction_id = f"admin-tts-voice-test-{uuid.uuid4()}"
            output_path = bundle.runtime.audio.new_tts_path(test_cfg, interaction_id)
            bundle.telemetry.log_event(
                EventType.ADMIN_TEST,
                "Admin TTS voice sample test requested.",
                component="admin",
                success=True,
                interaction_id=interaction_id,
                data={"voice": voice, "text_length": len(request.text)},
            )
            try:
                audio_path = await bundle.runtime.tts_factory(test_cfg).synthesize(request.text, output_path)
                await bundle.runtime.audio.play_file(test_cfg, audio_path, require_playback=True)
            except Exception as exc:
                bundle.telemetry.log_event(
                    EventType.ADMIN_TEST,
                    "Admin TTS voice sample test failed.",
                    component="admin",
                    success=False,
                    interaction_id=interaction_id,
                    error=_public_error(exc),
                    data={"voice": voice, "text_length": len(request.text)},
                )
                raise HTTPException(status_code=502, detail={"message": "TTS voice sample generation or playback failed.", "error": _public_error(exc)}) from exc
            bundle.telemetry.log_event(
                EventType.ADMIN_TEST,
                "Admin TTS voice sample test completed through physical speaker path.",
                component="admin",
                success=True,
                interaction_id=interaction_id,
                data={"voice": voice, "tts_path": str(audio_path)},
            )
            return {
                "status": "ok",
                "voice": voice,
                "persisted": False,
                "played_through": "assistant_physical_output",
                "tts_path": str(audio_path),
                "message": "Generated the sample with the selected Kokoro voice and played it through the configured assistant speaker output.",
            }

    @app.post("/api/tts-voices/apply")
    async def apply_tts_voice(request: TtsVoiceApplyRequest) -> dict[str, Any]:
        voice = _validate_voice_or_400(request.voice)
        try:
            phrases = normalize_generated_tts_phrases(request.phrases) if request.phrases is not None else None
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"message": "Invalid generated TTS sound phrase list.", "error": _public_error(exc)}) from exc
        if bundle.tts_voice_operation_lock.locked():
            raise HTTPException(status_code=409, detail={"message": "Another TTS voice operation is already running."})
        async with bundle.tts_voice_operation_lock:
            config_result = None
            config_updated = False
            try:
                saved_data = bundle.config_store.get_saved().public_dict()
                saved_data["services"]["tts"]["voice"] = voice
                if phrases is not None:
                    saved_data["sounds"]["generated_tts_phrases"] = phrases
                config_result = bundle.config_store.apply_config(saved_data)
                config_updated = True
                await bundle.runtime.reload_runtime_components()
                bundle.telemetry.log_event(
                    EventType.CONFIG,
                    "TTS voice committed to saved configuration.",
                    component="admin",
                    success=True,
                    data={
                        "voice": voice,
                        "configuration_path": "services.tts.voice",
                        "generated_tts_phrases_updated": phrases is not None,
                        "generated_tts_phrase_count": len(phrases or bundle.config_store.get_active().sounds.generated_tts_phrases),
                        "pending_restart_paths": config_result.pending_restart_paths,
                        "applied_runtime_paths": config_result.applied_runtime_paths,
                    },
                )

                cfg = bundle.config_store.get_active()
                phrases_to_generate = phrases or list(cfg.sounds.generated_tts_phrases)
                bundle.telemetry.log_event(
                    EventType.SOUND,
                    "TTS-generated assistant sound regeneration started.",
                    component="admin",
                    success=True,
                    data={"voice": voice, "phrase_count": len(phrases_to_generate), "phrases": phrases_to_generate},
                )
                regeneration = await regenerate_generated_tts_sounds(cfg, bundle.runtime.tts_factory, voice=voice, phrases=phrases_to_generate)
            except ValidationError as exc:
                _raise_config_validation_error(exc)
            except Exception as exc:
                event_type = EventType.SOUND if config_updated else EventType.CONFIG
                bundle.telemetry.log_event(
                    event_type,
                    "TTS voice commit or generated sound regeneration failed.",
                    component="admin",
                    success=False,
                    error=_public_error(exc),
                    data={"voice": voice, "configuration_updated": config_updated, "generated_tts_phrases_updated": phrases is not None},
                )
                raise HTTPException(
                    status_code=502,
                    detail={
                        "message": "TTS voice commit or generated sound regeneration failed.",
                        "error": _public_error(exc),
                        "configuration_updated": config_updated,
                        "configuration_result": config_result.model_dump(mode="json") if config_result else None,
                    },
                ) from exc
            bundle.telemetry.log_event(
                EventType.SOUND,
                "TTS-generated assistant sound regeneration completed.",
                component="admin",
                success=True,
                data={"voice": voice, "generated_count": regeneration["generated_count"], "files": [item["filename"] for item in regeneration["generated_files"]]},
            )
            return {
                "status": "ok",
                "voice": voice,
                "configuration_path": "services.tts.voice",
                "generated_tts_phrases": phrases_to_generate,
                "configuration_result": config_result.model_dump(mode="json"),
                "regeneration": regeneration,
                "message": "Committed the selected Kokoro voice and regenerated the listed generated assistant sound files.",
            }

    @app.get("/api/telemetry/events")
    async def events(
        event_type: str | None = None,
        start: str | None = None,
        end: str | None = None,
        errors_only: bool = False,
        conversation_id: str | None = None,
        interaction_id: str | None = None,
        component: str | None = None,
        command_intent: str | None = None,
        stage: str | None = None,
        search: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> dict[str, Any]:
        rows = bundle.telemetry.query_events(
            TelemetryFilters(
                event_type=event_type,
                start=start,
                end=end,
                errors_only=errors_only,
                conversation_id=conversation_id,
                interaction_id=interaction_id,
                component=component,
                command_intent=command_intent,
                stage=stage,
                search=search,
                limit=limit,
                offset=offset,
            )
        )
        return {"events": [row.model_dump(mode="json") for row in rows]}

    @app.get("/api/telemetry/live")
    async def live_events() -> StreamingResponse:
        queue = bundle.telemetry.subscribe()

        async def generator():
            try:
                while True:
                    event = await queue.get()
                    yield f"data: {event.model_dump_json()}\n\n"
            finally:
                bundle.telemetry.unsubscribe(queue)

        return StreamingResponse(generator(), media_type="text/event-stream")

    @app.get("/api/artifacts")
    async def list_artifacts(kind: str | None = None, conversation_id: str | None = None, interaction_id: str | None = None) -> dict[str, Any]:
        artifacts = bundle.telemetry.list_artifacts(kind=kind, conversation_id=conversation_id, interaction_id=interaction_id)
        return {"artifacts": [artifact.model_dump(mode="json") for artifact in artifacts]}

    @app.get("/api/artifacts/{artifact_id}/download")
    async def download_artifact(artifact_id: str) -> FileResponse:
        artifact = bundle.telemetry.get_artifact(artifact_id)
        if not artifact or not Path(artifact.path).exists():
            raise HTTPException(status_code=404, detail="artifact not found")
        return FileResponse(artifact.path, media_type=artifact.content_type, filename=artifact.filename)

    @app.get("/api/sounds")
    async def list_sounds() -> dict[str, Any]:
        cfg = bundle.config_store.get_active()
        sound_dir = _sound_dir(cfg)
        files = sorted([p.name for p in sound_dir.iterdir() if p.is_file()])
        return {
            "sound_directory": str(sound_dir),
            "files": files,
            "event_files": {str(k): v for k, v in cfg.sounds.event_files.items()},
            "format_guidance": "Use WAV files, preferably simple/uncompressed PCM WAV. Set an event file to an empty string to disable sound for that event. Test playback after upload.",
        }

    @app.post("/api/sounds")
    async def upload_sound(file: UploadFile = File(...)) -> dict[str, Any]:
        cfg = bundle.config_store.get_active()
        sound_dir = _sound_dir(cfg)
        filename = _safe_filename(file.filename or "uploaded.wav")
        dest = sound_dir / filename
        with dest.open("wb") as handle:
            shutil.copyfileobj(file.file, handle)
        bundle.telemetry.log_event(EventType.SOUND, "Sound file uploaded.", component="admin", success=True, data={"filename": filename})
        return {"filename": filename, "path": str(dest)}

    @app.delete("/api/sounds/{filename}")
    async def delete_sound(filename: str) -> dict[str, Any]:
        cfg = bundle.config_store.get_active()
        safe = _safe_filename(filename)
        path = _sound_dir(cfg) / safe
        if not path.exists():
            raise HTTPException(status_code=404, detail="sound not found")
        path.unlink()
        bundle.telemetry.log_event(EventType.SOUND, "Sound file deleted.", component="admin", success=True, data={"filename": safe})
        return {"deleted": safe}

    @app.post("/api/sounds/{filename}/play")
    async def play_sound(filename: str) -> dict[str, Any]:
        cfg = bundle.config_store.get_active()
        path = _sound_dir(cfg) / _safe_filename(filename)
        if not path.exists():
            raise HTTPException(status_code=404, detail="sound not found")
        await bundle.runtime.audio.play_file(cfg, path)
        bundle.telemetry.log_event(EventType.SOUND, "Sound file playback test completed.", component="admin", success=True, data={"filename": path.name})
        return {"played": path.name}

    @app.post("/api/sound-events/{event}/play")
    async def play_sound_event(event: SoundEvent) -> dict[str, Any]:
        cfg = bundle.config_store.get_active()
        await bundle.runtime.audio.play_sound_event(cfg, event)
        bundle.telemetry.log_event(EventType.SOUND, "Sound event playback test completed.", component="admin", success=True, data={"event": event.value})
        return {"played_event": event.value}

    @app.post("/api/test/wake")
    async def wake_test(request: WakeTestRequest) -> dict[str, Any]:
        detection = await bundle.runtime.simulate_wake(request.confidence)
        bundle.telemetry.log_event(EventType.ADMIN_TEST, "Admin simulated wake-word test triggered; not a production input source.", component="admin", success=True, data=detection.__dict__)
        return {
            "triggered": detection.__dict__,
            "diagnostic_only": True,
            "message": "This endpoint injects a simulated/admin wake event for diagnostics. Normal production use must be voice-only through the configured local wake engine.",
        }

    @app.post("/api/test/command-recognition")
    async def command_test(request: CommandRecognitionTestRequest) -> dict[str, Any]:
        cfg = bundle.config_store.get_active()
        registry = CommandRegistry(cfg.command_registry)
        match = registry.match_text(request.text)
        bundle.telemetry.log_event(EventType.ADMIN_TEST, "Admin command recognition test completed.", component="admin", success=True, command_intent=match.intent if match else None, data={"text": request.text, "matched": bool(match)})
        return {"matched": match.__dict__ if match else None}

    @app.post("/api/test/microphone")
    async def microphone_test(request: MicrophoneTestRequest) -> dict[str, Any]:
        cfg = bundle.config_store.get_active()
        interaction_id = f"admin-mic-{uuid.uuid4()}"
        path = Path(cfg.storage.artifacts_dir) / "scratch" / f"{interaction_id}.wav"
        capture = await bundle.runtime.audio.record_fixed_duration(cfg, request.duration_seconds, path)
        artifact = None
        if cfg.telemetry.audio_artifact_storage_enabled:
            artifact = bundle.telemetry.create_artifact(capture.path, ArtifactKind.ADMIN_MIC_TEST, interaction_id=interaction_id)
        bundle.telemetry.log_event(
            EventType.ADMIN_TEST,
            "Admin microphone test recording completed.",
            component="admin",
            success=True,
            interaction_id=interaction_id,
            data={"capture": capture.__dict__, "artifact_id": artifact.id if artifact else None},
        )
        return {"capture": capture.__dict__, "artifact": artifact.model_dump(mode="json") if artifact else None}

    @app.post("/api/test/llm-tts")
    async def llm_tts_test(request: LlmTtsTestRequest) -> dict[str, Any]:
        cfg = bundle.config_store.get_active()
        interaction_id = f"admin-llm-tts-{uuid.uuid4()}"
        messages = [{"role": "system", "content": cfg.conversation.system_prompt}, {"role": "user", "content": request.text}]
        llm_text = await bundle.runtime.llm_factory(cfg).chat(messages)
        tts_path = bundle.runtime.audio.new_tts_path(cfg, interaction_id)
        audio_path = await bundle.runtime.tts_factory(cfg).synthesize(llm_text, tts_path)
        await bundle.runtime.audio.play_file(cfg, audio_path)
        bundle.telemetry.log_event(
            EventType.ADMIN_TEST,
            "Admin typed LLM/TTS test completed through physical speaker path.",
            component="admin",
            success=True,
            interaction_id=interaction_id,
            data={"input": request.text, "assistant_response": llm_text, "tts_path": str(audio_path)},
        )
        return {"assistant_response": llm_text, "tts_path": str(audio_path)}

    @app.post("/api/maintenance/cleanup")
    async def cleanup() -> dict[str, Any]:
        return await bundle.maintenance.run_cleanup(bundle.config_store.get_active())

    @app.post("/api/maintenance/restart-service")
    async def restart_service(request: ConfirmRequest) -> dict[str, Any]:
        result = await bundle.maintenance.restart_service(bundle.config_store.get_active(), confirm=request.confirm)
        return result.__dict__

    @app.post("/api/maintenance/reboot")
    async def reboot(request: ConfirmRequest) -> dict[str, Any]:
        result = await bundle.maintenance.reboot_machine(bundle.config_store.get_active(), confirm=request.confirm)
        return result.__dict__

    return app


app = create_app()
