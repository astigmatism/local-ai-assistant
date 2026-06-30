# Operations Guide

## First deployment checklist

1. Confirm speakerphone hardware appears on the host:

```bash
lsusb
cat /proc/asound/cards
arecord -l
aplay -l
```

2. Confirm host user/container can access `/dev/snd`.

3. Reset speakerphone mixer volume after USB movement or reboot:

```bash
amixer -c 0 sset PCM 100% unmute
```

4. Create `.env` from `.env.example` and add real service keys.

5. Start the container:

```bash
docker compose up -d --build
```

6. Open the portal:

```text
http://<assistant-ip>:8080/
```

7. Run `/api/health` or the portal status/health controls.

8. Verify the packaged production wake engine is active:

```bash
curl -sS http://<assistant-ip>:8080/api/status
curl -sS http://<assistant-ip>:8080/api/health
```

`wake_engine` should be `external_command`, and the wake status should show `mode: production_local_subprocess`.

9. If this is an upgrade and the persisted config still says `wake.engine = simulated`, migrate it with the admin API:

```bash
curl -sS -X POST http://<assistant-ip>:8080/api/config/migrate-production-wake \
  -H 'content-type: application/json' \
  -d '{"confirm":true}'
```

10. Configure the local command recognizer for production command audio if desired. Vosk is supported with a local model path. The default configured-text recognizer is intended for tests and diagnostics.

11. Use the microphone test, sound tests, command-recognition test, typed LLM/TTS test, and then a voice-only `Rosalina` wake test before enabling unattended use.

## Production wake verification

The production wake process is packaged as:

```text
python -m voice_assistant.pocketsphinx_wake
```

Useful checks:

```bash
docker compose exec voice-assistant python -m voice_assistant.pocketsphinx_wake --self-test
curl -sS http://<assistant-ip>:8080/api/wake/debug
```

The self-test should report `engine: pocketsphinx_continuous_arecord_overlap`, `capture_backend: arecord_stream_overlap`, `window_seconds: 4.0`, `hop_seconds: 1.0`, `overlap_seconds: 3.0`, and `cooldown_seconds: 1.5`. `/api/status` should show `wake.process_running: true`; if the subprocess cannot keep running, inspect `wake.last_error` and `wake.stderr_tail` for the most recent `arecord` or PocketSphinx wrapper error.

Manual voice-only validation on the EMEET speakerphone:

1. Say `Rosalina` without pressing keys, opening the portal, using SSH, or posting to `/api/test/wake`.
2. Hear the full wake acknowledgement.
3. Ask a short question after the acknowledgement finishes.
4. Hear the response.
5. Check telemetry for wake, prompt capture, local command gate, STT, LLM, TTS, and playback events.
6. Reboot the thin client and repeat the same voice-only test.

During wake acknowledgement and prompt capture, the app pauses the wake subprocess to avoid self-triggering on acknowledgement audio and to release the ALSA microphone for prompt capture. It resumes wake listening after capture, so saying the wake phrase during STT, LLM, TTS, or playback acts as barge-in.

## Service restart and reboot

The admin portal exposes restart and reboot actions, but host command execution is disabled by default inside the container. To use those actions, set these config values after reviewing the host security model:

```json
{
  "maintenance": {
    "host_command_execution_enabled": true,
    "assistant_restart_command": ["systemctl", "restart", "voice-assistant"],
    "machine_reboot_command": ["sudo", "/sbin/reboot"]
  }
}
```

A systemd unit is provided in `deploy/systemd.service` to autostart the Docker Compose service after reboot.

## Retention and cleanup

Telemetry retention defaults to 365 days. Cleanup runs daily at the configured time and may also be triggered manually from `/api/maintenance/cleanup`.

Artifact storage can be disabled with:

```json
{
  "telemetry": {
    "audio_artifact_storage_enabled": false
  }
}
```

Telemetry events continue to be stored even when WAV artifacts are disabled.

## Troubleshooting

### Wake word does not trigger

```bash
curl -sS http://<assistant-ip>:8080/api/status
curl -sS http://<assistant-ip>:8080/api/health
docker compose exec voice-assistant python -m voice_assistant.pocketsphinx_wake --self-test
```

Confirm that `wake_engine` is not `simulated`, that the external wake command is available, and that the runtime/process is running. If `/api/status` still shows `simulated` after an upgrade, run the migration endpoint shown above.

### No audible playback

```bash
amixer -c 0
amixer -c 0 sset PCM 100% unmute
aplay -D plughw:0,0 assets/sounds/wake_ack.wav
```

### Microphone capture fails

```bash
arecord -D plughw:0,0 -f S16_LE -r 16000 -c 1 -d 5 /tmp/mic-test.wav
file /tmp/mic-test.wav
```

### STT 401/403

Check `WHISPER_API_KEY` exists in `.env` and was loaded by Docker Compose.

### TTS 401/403

Check `TTS_ROUTER_API_KEY` exists in `.env` and was loaded by Docker Compose.

### LLM router failure

The client must use `http://192.168.1.21:11434/api/chat`, not the old `8001` gateway and not the admin port `11435`. The assistant never sends an LLM model field.

### Admin portal exposure

There is intentionally no authentication. Use local LAN firewalling and do not publish port 8080 to the internet.
