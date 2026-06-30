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

8. Configure the production wake engine:

- `openwakeword` with a local model path; or
- `external_command` pointing at a dedicated local wake engine.

9. Configure the local command recognizer for production command audio. Vosk is supported with a local model path. The default configured-text recognizer is intended for tests and diagnostics.

10. Use the microphone test, sound tests, command-recognition test, and typed LLM/TTS test before enabling always-on unattended use.

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
