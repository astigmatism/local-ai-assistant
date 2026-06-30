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
from pydantic import BaseModel, Field

from .assistant import AssistantRuntime
from .commands import CommandRegistry
from .config import AssistantConfig, ConfigStore, load_config_store_from_env
from .constants import ArtifactKind, EventType, SoundEvent
from .health import HealthChecker
from .maintenance import MaintenanceController
from .telemetry import TelemetryFilters, TelemetryStore


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


INDEX_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Local Voice Assistant Admin</title>
  <style>
    body { font-family: system-ui, sans-serif; margin: 2rem; line-height: 1.45; }
    section { border: 1px solid #ddd; padding: 1rem; margin: 1rem 0; border-radius: 0.5rem; }
    textarea { width: 100%; min-height: 12rem; font-family: ui-monospace, monospace; }
    input, button, select { margin: 0.2rem; padding: 0.35rem; }
    pre { background: #f6f6f6; padding: 1rem; overflow: auto; }
    .warn { color: #8a4b00; }
  </style>
</head>
<body>
  <h1>Local Voice Assistant Admin</h1>
  <p class="warn">V1 has no authentication by requirement. Keep this service on the trusted local network only.</p>
  <section>
    <h2>Status</h2>
    <button onclick="loadStatus()">Refresh status</button>
    <button onclick="simulateWake()">Simulate wake (admin-only)</button>
    <button onclick="loadHealth()">Refresh health</button>
    <button onclick="loadWakeDebug()">Wake debug</button>
    <button onclick="migrateProductionWake()">Migrate saved config to production wake</button>
    <pre id="status"></pre>
    <pre id="health"></pre>
  </section>
  <section>
    <h2>Configuration</h2>
    <button onclick="loadConfig()">Load config</button>
    <button onclick="saveDraft()">Save draft</button>
    <button onclick="applyDraft()">Apply saved draft</button>
    <a href="/api/config/export" download="voice-assistant-config.json">Export saved config</a>
    <p>Edits are applied as a group. STT/LLM/TTS connection changes are saved as pending-restart values.</p>
    <textarea id="config"></textarea>
    <pre id="configResult"></pre>
  </section>
  <section>
    <h2>Sound library</h2>
    <p>Upload WAV files, preferably simple uncompressed PCM WAV. V1 intentionally performs only light filename checks; use playback tests to verify audio. Set a sound event file to an empty string to intentionally disable sound for that event.</p>
    <input id="soundFile" type="file" /> <button onclick="uploadSound()">Upload</button>
    <button onclick="loadSounds()">List sounds</button>
    <pre id="sounds"></pre>
  </section>
  <section>
    <h2>Diagnostics</h2>
    <input id="testText" value="Say this through the assistant path." size="60" />
    <button onclick="llmTtsTest()">Typed LLM/TTS test through speakerphone</button>
    <button onclick="micTest()">5s microphone test</button>
    <input id="commandText" value="stop" /> <button onclick="commandTest()">Local command test</button>
    <pre id="tests"></pre>
  </section>
  <section>
    <h2>Telemetry</h2>
    <input id="search" placeholder="search" /> <button onclick="loadEvents()">Search history</button>
    <pre id="events"></pre>
  </section>
<script>
async function j(url, opts={}) { const r = await fetch(url, opts); const t = await r.text(); try { return JSON.parse(t); } catch { return t; } }
async function loadStatus(){ document.getElementById('status').textContent = JSON.stringify(await j('/api/status'), null, 2); }
async function loadHealth(){ document.getElementById('health').textContent = JSON.stringify(await j('/api/health'), null, 2); }
async function loadWakeDebug(){ document.getElementById('health').textContent = JSON.stringify(await j('/api/wake/debug'), null, 2); }
async function migrateProductionWake(){ if(!confirm('Update saved config to the packaged local PocketSphinx production wake engine?')) return; document.getElementById('configResult').textContent = JSON.stringify(await j('/api/config/migrate-production-wake', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({confirm:true})}), null, 2); await loadStatus(); await loadConfig(); }
async function simulateWake(){ document.getElementById('status').textContent = JSON.stringify(await j('/api/test/wake', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({confidence:1})}), null, 2); }
async function loadConfig(){ const data = await j('/api/config'); document.getElementById('config').value = JSON.stringify(data.saved, null, 2); document.getElementById('configResult').textContent = JSON.stringify(data, null, 2); }
async function saveDraft(){ const body = JSON.parse(document.getElementById('config').value); document.getElementById('configResult').textContent = JSON.stringify(await j('/api/config/draft', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify(body)}), null, 2); }
async function applyDraft(){ document.getElementById('configResult').textContent = JSON.stringify(await j('/api/config/apply', {method:'POST', headers:{'content-type':'application/json'}, body:'{}'}), null, 2); }
async function uploadSound(){ const fd = new FormData(); const f = document.getElementById('soundFile').files[0]; fd.append('file', f); document.getElementById('sounds').textContent = JSON.stringify(await j('/api/sounds', {method:'POST', body:fd}), null, 2); }
async function loadSounds(){ document.getElementById('sounds').textContent = JSON.stringify(await j('/api/sounds'), null, 2); }
async function llmTtsTest(){ document.getElementById('tests').textContent = JSON.stringify(await j('/api/test/llm-tts', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({text:document.getElementById('testText').value})}), null, 2); }
async function micTest(){ document.getElementById('tests').textContent = JSON.stringify(await j('/api/test/microphone', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({duration_seconds:5})}), null, 2); }
async function commandTest(){ document.getElementById('tests').textContent = JSON.stringify(await j('/api/test/command-recognition', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({text:document.getElementById('commandText').value})}), null, 2); }
async function loadEvents(){ const q = encodeURIComponent(document.getElementById('search').value); document.getElementById('events').textContent = JSON.stringify(await j('/api/telemetry/events?search='+q), null, 2); }
loadStatus(); loadConfig(); loadEvents(); loadSounds();
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
        return HTMLResponse(INDEX_HTML)

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
        draft = bundle.config_store.save_draft(patch)
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
        draft = bundle.config_store.import_to_draft(imported)
        bundle.telemetry.log_event(EventType.CONFIG, "Configuration imported to draft.", component="admin", success=True)
        return {"draft": draft.public_dict(), "message": "Imported configuration saved as draft; use apply to persist it."}

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
