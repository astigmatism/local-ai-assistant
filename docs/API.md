# Admin API

The admin API has no authentication by v1 requirement. Keep it on a trusted local network.

## Status and health

```http
GET /api/status
GET /api/health
```

`/api/status` returns runtime state and current conversation metadata. `/api/health` checks wake engine availability, command recognizer, ALSA capture/playback, mixer volume, STT, LLM, and TTS reachability.

## Configuration

```http
GET  /api/config
POST /api/config/draft
POST /api/config/apply
GET  /api/config/export
POST /api/config/import
```

Draft/apply is grouped. `POST /api/config/apply` accepts either:

```json
{}
```

to apply the saved draft, or:

```json
{"config": {"full": "config object"}}
```

to apply a full config object directly.

The response contains:

```json
{
  "active": {},
  "saved": {},
  "pending_restart_paths": [],
  "applied_runtime_paths": []
}
```

## Telemetry and artifacts

```http
GET /api/telemetry/events
GET /api/telemetry/live
GET /api/artifacts
GET /api/artifacts/{artifact_id}/download
```

Supported telemetry filters:

```text
event_type
start
end
errors_only
conversation_id
interaction_id
component
command_intent
stage
search
limit
offset
```

`/api/telemetry/live` is a Server-Sent Events stream.

## Sound management

```http
GET    /api/sounds
POST   /api/sounds
DELETE /api/sounds/{filename}
POST   /api/sounds/{filename}/play
POST   /api/sound-events/{event}/play
```

V1 provides light filename safety and playback tests rather than deep WAV validation. Use simple PCM WAV files.

## Diagnostics

```http
POST /api/test/wake
POST /api/test/command-recognition
POST /api/test/microphone
POST /api/test/llm-tts
```

The typed LLM/TTS diagnostic sends text through the LLM and TTS path and plays generated speech through the thin client's speakerphone, not merely in the browser.

## Maintenance

```http
POST /api/maintenance/cleanup
POST /api/maintenance/restart-service
POST /api/maintenance/reboot
```

Restart and reboot require:

```json
{"confirm": true}
```

Host command execution is disabled by default in configuration. Enable it only after reviewing host permissions and commands.
