# Admin API

The admin API has no authentication by v1 requirement. Keep it on a trusted local network.

## Status and health

```http
GET /api/status
GET /api/health
GET /api/wake/debug
```

`/api/status` returns runtime state, current conversation metadata, active wake engine, the configured wake phrase, production-vs-simulated mode, and wake process status. For the packaged `external_command` production engine, the wake status includes `active_wake_phrase: Rosalina`, `process_running`, `packaged_backend`, `capture_backend`, `capture_device`, `sample_rate_hz`, `channels`, `threshold`, `window_seconds`, `hop_seconds`, `overlap_seconds`, `cooldown_seconds`, `restart_count`, `detection_count`, `last_raw_line`, `last_error`, `last_stderr_line`, and `stderr_tail` so ALSA/PocketSphinx subprocess failures are visible instead of being hidden behind a generic exit code. `/api/health` checks wake engine availability, wake runtime state, command recognizer, ALSA capture/playback, mixer volume, STT, LLM, and TTS reachability. `/api/wake/debug` returns wake status plus recent wake/barge-in telemetry and labels `/api/test/wake` as simulated/admin-only.

## Configuration

```http
GET  /api/config
POST /api/config/draft
POST /api/config/apply
POST /api/config/migrate-production-wake
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

To migrate an existing persisted simulated-wake deployment to the packaged production wake engine:

```http
POST /api/config/migrate-production-wake
```

Body:

```json
{"confirm": true}
```

This updates only the saved wake source fields, preserves unrelated settings, writes `data/config.json`, and reloads the runtime wake listener.

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

`POST /api/test/wake` injects a simulated/admin wake event and is diagnostic-only. It is not the normal production input source.

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
