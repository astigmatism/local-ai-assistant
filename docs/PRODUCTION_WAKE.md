# Production wake-word deployment: PocketSphinx external command

## Selected approach

This package now uses the existing `external_command` wake adapter as the production path, with a source-controlled command module:

```text
python -m voice_assistant.pocketsphinx_wake
```

The command wraps the local `pocketsphinx_continuous` keyword spotter, listens to the configured ALSA capture device, and emits one JSON line on stdout for each accepted wake detection. The main assistant supervises that subprocess, parses the JSON event, plays the wake acknowledgement, captures the prompt, and then resumes wake listening for barge-in during STT/LLM/TTS/playback.

This approach was selected because it avoids the failed `openwakeword` optional dependency path under the current Python 3.12 image, keeps wake detection local, uses Debian packages installed at image build time, and preserves the existing `external_command` abstraction for future wake engines.

## Changed behavior

Fresh deployments now default to:

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

The `simulated` engine and `POST /api/test/wake` remain available, but they are labeled as admin/test diagnostics and are no longer represented as the production input method.

## Build and deploy on the thin client

From the repository checkout on `local-ai-assistant-1`:

```bash
cd ~/apps/local-voice-assistant
cp -n .env.example .env
chmod 600 .env
# edit .env and set WHISPER_API_KEY and TTS_ROUTER_API_KEY

docker compose build
docker compose up -d
```

The Dockerfile installs `alsa-utils`, `pocketsphinx`, and `pocketsphinx-en-us`. Docker Compose keeps the existing deployment shape: host networking, `/dev/snd` passthrough, the `audio` group, `./data` persistence, and restart policy `unless-stopped`.

The expected target values remain:

```text
Admin URL: http://192.168.1.23:8080
ALSA capture: plughw:0,0
ALSA playback: plughw:0,0
STT: http://192.168.1.22:9000/v1/audio/transcriptions
LLM router: http://192.168.1.21:11434/api/chat
TTS router: http://192.168.1.22:8000/v1/audio/speech
TTS model/voice: kokoro / af_heart
```

## Upgrade/migrate an existing simulated-wake deployment

Existing deployments may have a persisted `data/config.json` with `wake.engine = simulated`. Because `data/` is a bind mount, rebuilding the image does not overwrite that file. Use the admin API migration endpoint after deploying the new image:

```bash
curl -sS -X POST http://192.168.1.23:8080/api/config/migrate-production-wake \
  -H 'content-type: application/json' \
  -d '{"confirm":true}'
```

The migration updates only the wake source fields, preserves unrelated saved settings, reloads the runtime wake listener, and writes the updated saved config back to `data/config.json`.

Equivalent result in `data/config.json`:

```json
{
  "wake": {
    "engine": "external_command",
    "external_command": ["python", "-m", "voice_assistant.pocketsphinx_wake"],
    "external_health_command": ["python", "-m", "voice_assistant.pocketsphinx_wake", "--self-test"],
    "active_wake_phrase": "computer"
  }
}
```

Do not edit tracked source files on the production machine to perform this migration.

## Verify production wake operation

After deployment or migration:

```bash
curl -sS http://192.168.1.23:8080/api/status
curl -sS http://192.168.1.23:8080/api/health
curl -sS http://192.168.1.23:8080/api/wake/debug
```

Expected signals:

- `/api/status` shows `wake_engine: external_command` and `wake.mode: production_local_subprocess`.
- `/api/status` shows the configured command `python -m voice_assistant.pocketsphinx_wake`.
- `/api/health` includes passing `wake-word engine` and `wake-word runtime` checks.
- `/api/wake/debug` shows recent real wake detections after voice tests and separately labels `/api/test/wake` as simulated/admin-only.

Voice-only hardware validation:

1. Say `computer` near the EMEET speakerphone.
2. Hear the wake acknowledgement sound.
3. Ask a simple question, for example: “What time is it?”
4. Hear the spoken answer through the EMEET speakerphone.
5. Confirm telemetry contains this sequence: `wake_detected -> prompt_capture_started -> prompt_capture_ended -> command_recognition_started -> stt_started -> llm_started -> tts_started -> playback_started -> playback_ended`.
6. Reboot the thin client.
7. Repeat the same voice-only test without opening the admin portal, pressing a key, running SSH commands, or posting to `/api/test/wake`.

## Audio/mixer note

The app still enforces the configured PCM mixer volume before playback. The known-good host boot workaround remains useful if the USB speakerphone resets volume after reboot or movement:

```bash
amixer -c 0 sset PCM 100% unmute
```

Keep `audio.enforce_pcm_volume_percent = 100` and `audio.mixer_card_index = 0` in app config for the current EMEET deployment.

## Privacy boundary

Pre-wake audio is consumed only by the local PocketSphinx wake subprocess. It is not sent to Whisper, the LLM router, TTS, or telemetry/artifact storage. Post-wake prompt audio continues to follow the existing telemetry and artifact-retention settings.

## Tuning

The app-level `wake.sensitivity` remains a 0.0 to 1.0 value. The wrapper maps it to PocketSphinx's keyword threshold. Higher values are more permissive. If needed, operators can tune the saved config through `/api/config` and verify with `/api/health` plus voice-only tests.

For advanced troubleshooting, run the wrapper self-test inside the container:

```bash
docker compose exec voice-assistant python -m voice_assistant.pocketsphinx_wake --self-test
```
