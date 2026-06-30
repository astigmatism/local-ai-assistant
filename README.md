# Local Voice Assistant Thin Client

A container-ready, voice-only, Alexa-like assistant service for an Ubuntu/Linux thin client with an attached USB speakerphone.

The service is responsible for local wake-word orchestration, post-wake prompt capture, local command gating, STT/LLM/TTS routing, playback, conversation context, telemetry, artifact retention, and a local-network admin portal with no authentication.

## What is included

- Python/FastAPI assistant service and browser admin portal.
- Explicit state-machine orchestration for wake, capture, local commands, STT, LLM, TTS, playback, errors, and barge-in.
- Configurable local wake-word engine abstraction:
  - `simulated` for tests/admin diagnostics.
  - `openwakeword` adapter for local openWakeWord microphone inference.
  - `external_command` adapter for a local wake-word process that emits detections; the packaged production command wraps local PocketSphinx keyword spotting for `computer`.
- Local command registry with whole-utterance matching and only the two required v1 intents by default: `cancel_stop` and `new_conversation`.
- Optional local Vosk command recognizer for command-audio transcription before main STT.
- OpenAI-compatible Whisper STT client.
- Ollama router chat client that deliberately sends **no LLM model field**.
- OpenAI-compatible Kokoro TTS router client.
- ALSA audio capture/playback implementation using `arecord`, `aplay`, and `amixer`.
- SQLite telemetry/history database.
- Optional WAV artifact storage and retention cleanup.
- Admin endpoints for configuration, import/export, sound upload/list/delete/play, telemetry filtering/search, live SSE events, artifacts, status, health, diagnostics, cleanup, restart, and reboot.
- Dockerfile, Docker Compose, systemd unit, default WAV sound effects, and pytest suite.

## Current LAN defaults

The default configuration matches the supplied integration handoff:

```text
STT_URL=http://192.168.1.22:9000/v1/audio/transcriptions
LLM_URL=http://192.168.1.21:11434/api/chat
LLM_HEALTH_URL=http://192.168.1.21:11434/health
TTS_URL=http://192.168.1.22:8000/v1/audio/speech
TTS_MODEL=kokoro
TTS_VOICE=af_heart
AUDIO_CAPTURE_DEVICE=plughw:0,0
AUDIO_PLAYBACK_DEVICE=plughw:0,0
AUDIO_CARD_INDEX=0
```

Secrets are read from environment variables and are not stored in source control:

```text
WHISPER_API_KEY
TTS_ROUTER_API_KEY
```

The LLM client calls the Ollama router with this shape:

```json
{
  "stream": false,
  "messages": [
    {"role": "system", "content": "..."},
    {"role": "user", "content": "..."}
  ]
}
```

There is intentionally no `model` field. Active model selection remains owned by the Ollama router/deployment stack.

## Runtime behavior

Normal voice flow:

```text
local wake detection
  -> wake acknowledgement sound and prompt capture start together
  -> prompt capture ends by silence or max duration
  -> local command recognizer checks the whole captured utterance
  -> non-command audio goes to STT
  -> valid STT text goes to LLM with current conversation context
  -> LLM text goes to TTS
  -> generated WAV plays through the speakerphone
  -> conversation inactivity timer starts after playback finishes
```

Barge-in flow:

```text
wake detected during STT, LLM, TTS, or playback
  -> active process/playback is cancelled
  -> wake acknowledgement sound plays
  -> new prompt capture starts
```

Wake words during prompt capture do not create a second wake event; they are treated as prompt audio.

## Configuration highlights

Default values include:

```text
minimum prompt capture duration: 3 seconds
maximum prompt duration: 120 seconds
conversation inactivity timeout: 60 seconds
telemetry retention: 365 days
cleanup schedule: daily at 03:00
```

All configurable settings are visible through `/api/config` and the browser portal. Configuration edits use a grouped draft/apply flow:

1. Save a full or partial draft with `POST /api/config/draft`.
2. Apply it with `POST /api/config/apply`.
3. Settings outside STT/LLM/TTS connection blocks become active immediately.
4. STT/LLM/TTS connection settings are persisted but remain pending until service restart.

There is no reset-to-defaults operation. Restore a known-good configuration by importing a previously exported configuration and applying it.

## Local command recognition

The default registry includes only:

- `cancel_stop`
- `new_conversation`

Whole-utterance matching is enforced. For example, `cancel` can match a cancel command, but `How do I cancel a Linux process?` does not.

For production command-audio recognition before main STT, configure the Vosk recognizer:

```json
{
  "command_registry": {
    "recognizer": {
      "engine": "vosk",
      "vosk_model_path": "/models/vosk-small-en-us",
      "confidence_threshold": 0.7
    }
  }
}
```

The `configured_text` recognizer is included for tests and admin diagnostics. It does not call the downstream/main STT service.

## Wake-word engine

The service does not use downstream STT as a wake detector. Wake detection is local and state-dependent.

Fresh production deployments default to the packaged local PocketSphinx external command:

```json
{
  "wake": {
    "engine": "external_command",
    "wake_phrases": ["computer"],
    "active_wake_phrase": "computer",
    "external_command": ["python", "-m", "voice_assistant.pocketsphinx_wake"],
    "external_health_command": ["python", "-m", "voice_assistant.pocketsphinx_wake", "--self-test"],
    "sensitivity": 0.5
  }
}
```

The Dockerfile installs `alsa-utils`, `pocketsphinx`, and `pocketsphinx-en-us` at image build time, so no wake model is downloaded at runtime. The packaged wake subprocess is long-running, but it does **not** keep one endless `arecord | pocketsphinx_continuous` pipe open. Instead, it repeatedly captures finite raw PCM windows with `arecord -d 4`, sends each window to `pocketsphinx_continuous -infile /dev/stdin`, parses the decoder output after PocketSphinx sees EOF, and starts the next window. This matches the target hardware result where finite chunks detected `computer` while the endless pipe stayed silent. It intentionally avoids PocketSphinx's `-inmic yes -adcdev ...` live microphone mode because that backend failed on the target EMEET/ALSA deployment. The subprocess reads only local microphone audio and emits JSON wake detections to the app with `engine: pocketsphinx_continuous_arecord_chunk`. During prompt capture the app pauses the wake subprocess so ALSA capture is owned by the prompt recorder; after capture it resumes wake listening for barge-in during STT, LLM, TTS, and playback.

`simulated` remains available for admin/test diagnostics through `POST /api/test/wake`, but status, health, and the admin portal label it as diagnostic-only. `openwakeword` remains as an optional adapter, but the packaged production path no longer depends on its Python 3.12 optional dependency stack.

Existing deployments with persisted `data/config.json` may still have `wake.engine = simulated`. After deploying this version, migrate the saved wake config without editing source files:

```bash
curl -sS -X POST http://192.168.1.23:8080/api/config/migrate-production-wake \
  -H 'content-type: application/json' \
  -d '{"confirm":true}'
```

See `docs/PRODUCTION_WAKE.md` for build, migration, voice-only verification, and tuning steps.

## Docker deployment

1. Copy the package to the assistant machine.
2. Create the environment file:

```bash
cp .env.example .env
chmod 600 .env
# edit .env and set WHISPER_API_KEY and TTS_ROUTER_API_KEY
```

3. Build and run:

```bash
docker compose up -d --build
```

4. Open the admin portal from the LAN:

```text
http://<assistant-thin-client-ip>:8080/
```

The Compose file uses `network_mode: host`, mounts `/dev/snd`, adds the `audio` group, and persists data under `./data`. The Dockerfile installs the packaged PocketSphinx wake detector dependencies by default.

For an upgrade from an older simulated-wake deployment, rebuild/redeploy first, then run:

```bash
curl -sS -X POST http://192.168.1.23:8080/api/config/migrate-production-wake \
  -H 'content-type: application/json' \
  -d '{"confirm":true}'
```

Verify with:

```bash
curl -sS http://192.168.1.23:8080/api/status
curl -sS http://192.168.1.23:8080/api/health
```

`/api/status` should show `wake_engine` as `external_command`, not `simulated`.
It should also show `wake.process_running` as `true` and `wake.packaged_backend` as `pocketsphinx_continuous_arecord_chunk`; if the subprocess exits, `wake.last_error` and `wake.stderr_tail` report the most recent wrapper/capture failure.

To build with optional openWakeWord and Vosk dependencies:

```bash
docker compose build \
  --build-arg INSTALL_WAKE_EXTRAS=true \
  --build-arg INSTALL_COMMAND_VOSK=true
```

## Host audio prerequisites

On the Ubuntu assistant machine, confirm the EMEET speakerphone is visible and the user/container has audio access:

```bash
lsusb
cat /proc/asound/cards
arecord -l
aplay -l
amixer -c 0
```

The known working ALSA device target is:

```text
plughw:0,0
```

If playback becomes silent after USB movement or reboot, reset mixer volume:

```bash
amixer -c 0 sset PCM 100% unmute
```

## Admin API summary

| Area | Endpoints |
|---|---|
| Portal | `GET /` |
| Status/health | `GET /api/status`, `GET /api/health`, `GET /api/wake/debug` |
| Config | `GET /api/config`, `POST /api/config/draft`, `POST /api/config/apply`, `POST /api/config/migrate-production-wake`, `GET /api/config/export`, `POST /api/config/import` |
| Telemetry | `GET /api/telemetry/events`, `GET /api/telemetry/live` |
| Artifacts | `GET /api/artifacts`, `GET /api/artifacts/{id}/download` |
| Sounds | `GET /api/sounds`, `POST /api/sounds`, `DELETE /api/sounds/{filename}`, `POST /api/sounds/{filename}/play`, `POST /api/sound-events/{event}/play` |
| Tests | `POST /api/test/wake`, `POST /api/test/command-recognition`, `POST /api/test/microphone`, `POST /api/test/llm-tts` |
| Maintenance | `POST /api/maintenance/cleanup`, `POST /api/maintenance/restart-service`, `POST /api/maintenance/reboot` |

Restart and reboot endpoints require `{"confirm": true}`. Host command execution is disabled by default inside the container. Enable it only after choosing safe host-specific commands and deployment permissions.

## Running tests

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[test]'
pytest
```

The test suite covers defaults, production wake config validation and migration, the rolling finite-window `arecord -d N` to PocketSphinx production wake wrapper, external wake stdout parsing/process termination/pause-resume/stderr diagnostics, health failures for missing wake dependencies/commands/runtime, config draft/apply/import/export, restart-pending service settings, local command whole-utterance matching, command recognition only after wake and prompt capture, conversation preservation/expiration/reset, normal prompt processing, invalid prompt handling, local commands, new conversation capture restart, LLM failure behavior, barge-in, prompt-capture wake handling, sound management, telemetry search, microphone artifacts, maintenance confirmations, and the LLM no-model router contract.

## Security posture

The admin portal intentionally has no authentication in v1. It exposes configuration, telemetry, transcripts, artifacts, diagnostic tools, restart controls, and reboot controls. Keep it on a trusted local network and do not expose it to the public internet.

## Project layout

```text
src/voice_assistant/   service source code
assets/sounds/         default local sound-effect WAV files
tests/                 pytest acceptance/unit tests
docs/                  design traceability and API/operations notes
deploy/                systemd unit for Docker Compose autostart
Dockerfile             container image definition
docker-compose.yml     host-network ALSA deployment example
```
