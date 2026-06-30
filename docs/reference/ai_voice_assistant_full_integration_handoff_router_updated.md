# AI Voice Assistant Systems Integration Handoff

**Updated:** 2026-06-29  
**Status:** End-to-end systems integration validated after Ollama router migration  
**Scope:** Hardware discovery, USB speakerphone audio validation, network Whisper speech-to-text, Kokoro text-to-speech, and Ollama-router-backed LLM response playback.

---

## 1. Executive Summary

A large portion of the local Alexa-like assistant integration has been validated on the home network.

The currently working end-to-end path is:

```text
EMEET USB speakerphone microphone
  -> local Ubuntu assistant machine records audio
  -> Whisper STT service transcribes speech
  -> Ollama router receives the prompt without a client-supplied model name
  -> Ollama router uses the active/preloaded model managed elsewhere
  -> LLM response text is returned
  -> Kokoro TTS generates speech audio
  -> EMEET USB speakerphone plays the response
```

The previous LLM path used the custom `local-ai-llm-legacy` gateway on port `8001`. That has been superseded by the new `local-ai-ollama-router` on the Ollama machine. The assistant script has been updated to call the Ollama-compatible router API directly at:

```text
http://192.168.1.21:11434/api/chat
```

The assistant must **not** send a model name. Model selection, model loading, prewarming, and active-model management are handled outside the assistant by the Ollama router/deployment stack.

---

## 2. Confirmed Machines and Roles

### 2.1 Assistant / speakerphone machine

**Host role:** Physical voice satellite / assistant edge node  
**Known shell prompt:** `astigmatism@local-ai-assistant-1`  
**Responsibilities:**

- USB speakerphone microphone capture
- USB speakerphone playback
- Thin Bash workflow testing
- Calls network STT, LLM router, and TTS services

### 2.2 STT/TTS machine

**IP:** `192.168.1.22`  
**Known shell prompt:** `astigmatism@local-ai-voice`  
**Responsibilities:**

- Whisper speech-to-text service
- TTS router service
- Kokoro TTS backend
- Chatterbox TTS backend exists but is not used for this assistant path

### 2.3 LLM / Ollama router machine

**IP:** `192.168.1.21`  
**Known shell prompt:** `astigmatism@rosalina`  
**Responsibilities:**

- Ollama runtime
- Ollama router
- Active/preloaded model management
- Router admin/status dashboard
- OpenWebUI also exists on this machine, but is explicitly **not** part of this assistant integration

---

## 3. Hardware Discovery and Validation

### 3.1 USB speakerphone

Detected hardware:

```text
EMEET OfficeCore M0 Plus
USB vendor/product: 328f:0109
```

The device enumerates as a USB audio card:

```text
card 0: Plus [EMEET OfficeCore M0 Plus], device 0: USB Audio [USB Audio]
```

The working ALSA device target is:

```text
plughw:0,0
```

### 3.2 User audio permissions

Initial issue: the Linux kernel saw `/dev/snd` nodes, but non-root ALSA tools could not list capture/playback devices.

Root cause: the SSH user was not in the `audio` group.

Resolution:

```bash
sudo usermod -aG audio "$USER"
```

Then SSH logout/login was required for group membership to apply.

Confirmed user group membership now includes:

```text
audio
```

### 3.3 Useful hardware inspection commands

```bash
printf '\n== USB devices ==\n'; lsusb
printf '\n== ALSA cards ==\n'; cat /proc/asound/cards
printf '\n== Capture devices ==\n'; arecord -l
printf '\n== Playback devices ==\n'; aplay -l
printf '\n== Mixer card 0 ==\n'; amixer -c 0
```

### 3.4 Microphone capture validation

Command used:

```bash
mkdir -p ~/audio-test
arecord -D plughw:0,0 -f S16_LE -r 16000 -c 1 -d 5 ~/audio-test/mic-test.wav
ls -lh ~/audio-test/mic-test.wav
file ~/audio-test/mic-test.wav
```

Expected file shape:

```text
RIFF WAVE audio, Microsoft PCM, 16 bit, mono 16000 Hz
```

A spoken recording test also used the `-vv` meter:

```bash
arecord -D plughw:0,0 -f S16_LE -r 16000 -c 1 -d 5 -vv ~/audio-test/spoken-test.wav
aplay -D plughw:0,0 ~/audio-test/spoken-test.wav
```

Result: microphone capture and local playback were confirmed.

### 3.5 Speaker playback validation

Speaker test command:

```bash
amixer -c 0 sset PCM 100% unmute
speaker-test -D plughw:0,0 -c 1 -t sine -f 440 -l 1
```

Result: test tone was heard.

### 3.6 Important USB replug behavior

Moving the EMEET speakerphone from a front USB port to a rear USB port reset the EMEET ALSA mixer playback volume to `0%`.

Observed mixer state after replug:

```text
Simple mixer control 'PCM',0
  Playback channels: Mono
  Limits: Playback 0 - 100
  Mono: Playback 0 [0%] [0.00dB] [on]
```

Fix:

```bash
amixer -c 0 sset PCM 100% unmute
```

Keep this command in future scripts or startup logic because USB movement or reboot may reset volume.

---

## 4. Speech-to-Text Integration

### 4.1 STT service container

On `192.168.1.22`, Docker showed:

```text
voice-whisper-stt   hwdsl2/whisper-server:cuda   192.168.1.22:9000->9000/tcp
```

### 4.2 STT API endpoint

The Whisper server exposes an OpenAI-compatible transcription endpoint:

```text
http://192.168.1.22:9000/v1/audio/transcriptions
```

It also exposes:

```text
http://192.168.1.22:9000/docs
http://192.168.1.22:9000/openapi.json
http://192.168.1.22:9000/v1/models
```

### 4.3 STT auth

The container uses:

```text
WHISPER_API_KEY
```

The key was retrieved from the Docker container environment on the STT/TTS machine, then stored locally on the assistant machine without echoing it to the terminal.

### 4.4 Local assistant config file

Config file on the assistant machine:

```text
~/.config/voice-test/whisper.env
```

Known variables stored there:

```text
WHISPER_API_KEY=<secret>
WHISPER_URL=http://192.168.1.22:9000/v1/audio/transcriptions
TTS_ROUTER_API_KEY=<secret>
TTS_URL=http://192.168.1.22:8000/v1/audio/speech
TTS_MODEL=kokoro
TTS_VOICE=af_heart
TTS_RESPONSE_FORMAT=wav
```

The file should remain mode `600`:

```bash
chmod 600 ~/.config/voice-test/whisper.env
```

Do not commit this file to source control.

### 4.5 Direct STT validation command

```bash
WHISPER_API_KEY="$(awk -F= '$1=="WHISPER_API_KEY"{sub(/^[^=]*=/,""); print; exit}' ~/.config/voice-test/whisper.env)"
WHISPER_URL="$(awk -F= '$1=="WHISPER_URL"{sub(/^[^=]*=/,""); print; exit}' ~/.config/voice-test/whisper.env)"

curl -sS --max-time 60 \
  -H "Authorization: Bearer ${WHISPER_API_KEY}" \
  -F "file=@${HOME}/audio-test/spoken-test.wav" \
  -F "model=whisper-1" \
  -F "response_format=json" \
  "$WHISPER_URL"

unset WHISPER_API_KEY WHISPER_URL
```

Validated output example:

```json
{"text":"Hello, this is Eric, and he's talking, and the microphone is recording."}
```

---

## 5. Text-to-Speech Integration

### 5.1 TTS containers

On `192.168.1.22`, Docker showed:

```text
voice-tts-router       openwebui-voice-stack-tts-router                 192.168.1.22:8000->8000/tcp
voice-kokoro-tts       ghcr.io/remsky/kokoro-fastapi-gpu:v0.5.0-cu126   127.0.0.1:8880->8880/tcp
voice-chatterbox-tts   openwebui-voice-stack-chatterbox-tts             127.0.0.1:4123->4123/tcp
```

The assistant path uses the exposed TTS router on port `8000`, not the local-only Kokoro backend directly.

### 5.2 TTS router endpoint

```text
http://192.168.1.22:8000/v1/audio/speech
```

The endpoint accepts OpenAI-compatible JSON:

```json
{
  "model": "kokoro",
  "voice": "af_heart",
  "input": "Text to speak",
  "response_format": "wav",
  "speed": 1.0,
  "stream": false,
  "volume_multiplier": 1.5
}
```

### 5.3 TTS auth

The TTS router uses:

```text
TTS_ROUTER_API_KEY
```

This was retrieved from `voice-tts-router` and stored in the same assistant-side config file:

```text
~/.config/voice-test/whisper.env
```

### 5.4 Voices

The TTS router lists Kokoro voices and Chatterbox voices. For this assistant integration, use only Kokoro voices.

Validated default voice:

```text
af_heart
```

Do not use Chatterbox voices for this baseline assistant path.

### 5.5 Direct TTS validation command

```bash
set -a
. ~/.config/voice-test/whisper.env
set +a

mkdir -p ~/audio-test

curl -f -sS -L --max-time 90 \
  -X POST "$TTS_URL" \
  -H "Authorization: Bearer ${TTS_ROUTER_API_KEY}" \
  -H "Content-Type: application/json" \
  -d "{\"model\":\"${TTS_MODEL}\",\"voice\":\"${TTS_VOICE}\",\"input\":\"Testing Kokoro text to speech through the EMEET speakerphone.\",\"response_format\":\"wav\",\"speed\":1.0,\"stream\":false,\"volume_multiplier\":1.5}" \
  -o ~/audio-test/kokoro-test-nonstream.wav

file ~/audio-test/kokoro-test-nonstream.wav
amixer -c 0 sset PCM 100% unmute
aplay -D plughw:0,0 ~/audio-test/kokoro-test-nonstream.wav
```

Validated WAV shape:

```text
RIFF WAVE audio, Microsoft PCM, 16 bit, mono 24000 Hz
```

---

## 6. LLM Integration After Ollama Router Migration

### 6.1 Previous LLM path, now obsolete

The older successful test used:

```text
http://192.168.1.21:8001/api/assistant/chat
```

That was provided by `local-ai-llm-legacy` after a model-safe assistant endpoint was added.

This path is now obsolete because the new deployment uses `local-ai-ollama-router`.

Observed failure from the old script:

```text
curl: (7) Failed to connect to 192.168.1.21 port 8001: Could not connect to server
```

### 6.2 Current LLM router containers

On `192.168.1.21`, Docker now shows:

```text
local-ai-ollama-router   local-ai-ollama-router:latest   192.168.1.21:11434-11435->11434-11435/tcp
local-ai-ollama          ollama/ollama:latest            11434/tcp
open-webui               ghcr.io/open-webui/open-webui   192.168.1.21:3000->8080/tcp
```

### 6.3 Current router ports

```text
192.168.1.21:11434 = Ollama-compatible API port
192.168.1.21:11435 = human/admin/status portal port
```

The admin port is not the API port. Ollama-compatible calls must use `11434`.

### 6.4 Router health endpoint

```bash
curl -sS --max-time 10 http://192.168.1.21:11434/health
```

The router health response includes:

```text
router appName: local-ai-ollama-router
upstream Ollama version: 0.30.10
activeModel loadedFrom: file
active model file: /app/runtime/active-model.json
source: local-ai-ollama-stack deploy-runtime.sh
```

This is important: the active model is controlled by the deployment/runtime marker, not by the assistant.

### 6.5 Active/preloaded model rule

The assistant must never tell Ollama which model to use.

The assistant must not send a `model` field to the LLM router.

The router owns this responsibility:

```text
client request has no model field
router reads active model marker
router calls upstream Ollama with active model internally
router enforces keep-alive/policy
router returns Ollama-compatible response
```

### 6.6 Routes that were tested

The router API port responded successfully to:

```text
GET  /                 -> Ollama is running
GET  /health           -> router/upstream/activeModel status
GET  /api/version      -> Ollama version
GET  /api/ps           -> currently loaded model list
GET  /api/tags         -> installed model list
POST /api/chat         -> working model-safe chat path when no model is supplied
```

The old custom route is not enabled on the router:

```text
GET /api/assistant/chat -> ROUTE_NOT_SUPPORTED
```

The admin port returns `API_NOT_ON_ADMIN_PORT` for Ollama-compatible API paths, confirming that `11435` is dashboard/admin only.

### 6.7 Validated no-model LLM test

This request includes **no model field**:

```bash
curl -sS -i --max-time 120 \
  -X POST "http://192.168.1.21:11434/api/chat" \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"Reply with exactly: router voice test ok"}],"stream":false}'
```

Validated response behavior:

```json
{
  "model": "hauhau-qwen3.6-35b-a3b-aggressive-q4-k-m:qwen35-parser",
  "message": {
    "role": "assistant",
    "content": "router voice test ok"
  },
  "done": true
}
```

Even though the response includes the model, the client did not send one. The router supplied/used the active model internally.

### 6.8 OpenWebUI exclusion

OpenWebUI exists on the LLM machine but is explicitly not part of this assistant architecture.

Do not integrate the assistant with:

```text
http://192.168.1.21:3000
```

Do not depend on OpenWebUI API keys, chat endpoints, sessions, users, or configuration.

OpenWebUI is unrelated infrastructure for this assistant path.

---

## 7. Current Working Scripts on Assistant Machine

Scripts live in:

```text
~/bin
```

### 7.1 `listen-and-transcribe`

Purpose:

```text
record microphone -> Whisper STT -> print transcript
```

Typical usage:

```bash
~/bin/listen-and-transcribe 5
```

### 7.2 `listen-transcribe-speak`

Purpose:

```text
record microphone -> Whisper STT -> Kokoro repeats the transcript -> EMEET speaker playback
```

Typical usage:

```bash
~/bin/listen-transcribe-speak 5
```

### 7.3 `listen-ask-speak`

Purpose:

```text
record microphone -> Whisper STT -> Ollama router LLM response -> Kokoro TTS -> EMEET speaker playback
```

Typical usage:

```bash
~/bin/listen-ask-speak 10
```

Current expected output flow:

```text
Listening for 10s...
Transcribing...

You said:
<transcript>

Asking Ollama router...

Assistant:
<llm response text>

Generating Kokoro speech...
Speaking...
```

---

## 8. Current `listen-ask-speak` Router-Compatible Script

The following is the current router-compatible shape. It sends no model field to the LLM router and extracts text from `message.content`.

```bash
#!/usr/bin/env bash
set -euo pipefail

DURATION="${1:-5}"
CONFIG="${VOICE_TEST_CONFIG:-$HOME/.config/voice-test/whisper.env}"
DEVICE="${VOICE_TEST_DEVICE:-plughw:0,0}"
LLM_URL="${LLM_URL:-http://192.168.1.21:11434/api/chat}"

if ! [[ "$DURATION" =~ ^[0-9]+$ ]] || [ "$DURATION" -lt 1 ]; then
  echo "Usage: listen-ask-speak [seconds]" >&2
  exit 1
fi

if [ ! -f "$CONFIG" ]; then
  echo "Missing config file: $CONFIG" >&2
  exit 1
fi

set -a
. "$CONFIG"
set +a

TMP_USER_WAV="$(mktemp --suffix=.wav)"
TMP_TTS_WAV="$(mktemp --suffix=.wav)"
TMP_LLM_REQUEST="$(mktemp --suffix=.json)"
TMP_LLM_RESPONSE="$(mktemp --suffix=.json)"
TMP_TTS_REQUEST="$(mktemp --suffix=.json)"
trap 'rm -f "$TMP_USER_WAV" "$TMP_TTS_WAV" "$TMP_LLM_REQUEST" "$TMP_LLM_RESPONSE" "$TMP_TTS_REQUEST"' EXIT

amixer -c 0 sset PCM 100% unmute >/dev/null

echo "Listening for ${DURATION}s..."
arecord -q -D "$DEVICE" -f S16_LE -r 16000 -c 1 -d "$DURATION" "$TMP_USER_WAV"

echo "Transcribing..."
TRANSCRIPT="$(
  curl -f -sS --max-time 60 \
    -H "Authorization: Bearer ${WHISPER_API_KEY}" \
    -F "file=@${TMP_USER_WAV}" \
    -F "model=whisper-1" \
    -F "response_format=text" \
    "$WHISPER_URL"
)"

TRANSCRIPT="$(printf '%s' "$TRANSCRIPT" | sed -E 's/^[[:space:]]+|[[:space:]]+$//g')"

if [ -z "$TRANSCRIPT" ]; then
  echo "No transcript returned."
  exit 1
fi

echo
echo "You said:"
echo "$TRANSCRIPT"
echo

python3 - "$TRANSCRIPT" > "$TMP_LLM_REQUEST" <<'PY'
import json
import sys

prompt = sys.argv[1]

print(json.dumps({
    "stream": False,
    "messages": [
        {
            "role": "system",
            "content": "You are a concise voice assistant. Answer naturally in one or two short sentences. Do not mention implementation details."
        },
        {
            "role": "user",
            "content": prompt
        }
    ]
}))
PY

echo "Asking Ollama router..."
curl -f -sS --max-time 180 \
  -X POST "$LLM_URL" \
  -H "Content-Type: application/json" \
  -d "@${TMP_LLM_REQUEST}" \
  -o "$TMP_LLM_RESPONSE"

LLM_TEXT="$(
  python3 - "$TMP_LLM_RESPONSE" <<'PY'
import json
import sys

path = sys.argv[1]
with open(path, "r", encoding="utf-8") as f:
    data = json.load(f)

message = data.get("message")
if not isinstance(message, dict):
    raise SystemExit("LLM response did not contain message object")

text = message.get("content")
if not isinstance(text, str) or not text.strip():
    raise SystemExit("LLM returned no message.content text")

print(text.strip())
PY
)"

echo
echo "Assistant:"
echo "$LLM_TEXT"
echo

python3 - "$LLM_TEXT" "$TTS_MODEL" "$TTS_VOICE" > "$TMP_TTS_REQUEST" <<'PY'
import json
import sys

text, model, voice = sys.argv[1], sys.argv[2], sys.argv[3]

print(json.dumps({
    "model": model,
    "voice": voice,
    "input": text,
    "response_format": "wav",
    "speed": 1.0,
    "stream": False,
    "volume_multiplier": 1.5
}))
PY

echo "Generating Kokoro speech..."
curl -f -sS -L --max-time 90 \
  -X POST "$TTS_URL" \
  -H "Authorization: Bearer ${TTS_ROUTER_API_KEY}" \
  -H "Content-Type: application/json" \
  -d "@${TMP_TTS_REQUEST}" \
  -o "$TMP_TTS_WAV"

echo "Speaking..."
aplay -q -D "$DEVICE" "$TMP_TTS_WAV"
```

---

## 9. Troubleshooting Guide

### 9.1 `curl: (7) Failed to connect to 192.168.1.21 port 8001`

Cause:

```text
The script is still using the old local-ai-llm-legacy gateway.
```

Fix:

```text
Use http://192.168.1.21:11434/api/chat instead.
```

### 9.2 `/api/assistant/chat` returns `ROUTE_NOT_SUPPORTED`

Cause:

```text
The new Ollama router does not enable the old custom assistant route.
```

Fix:

```text
Use POST /api/chat with Ollama-style messages and no model field.
```

### 9.3 Admin port returns `API_NOT_ON_ADMIN_PORT`

Cause:

```text
The request was sent to 11435.
```

Fix:

```text
Use 11434 for Ollama-compatible API calls.
Use 11435 only for admin dashboard/status UI.
```

### 9.4 No speaker output after USB move or reboot

Likely cause:

```text
EMEET PCM volume reset to 0%.
```

Fix:

```bash
amixer -c 0 sset PCM 100% unmute
```

### 9.5 `arecord` or `aplay` cannot see soundcards as normal user

Likely cause:

```text
User is not in audio group, or SSH session predates group change.
```

Fix:

```bash
sudo usermod -aG audio "$USER"
exit
# reconnect SSH
```

### 9.6 STT returns 401 Unauthorized

Likely cause:

```text
WHISPER_API_KEY missing or wrong in ~/.config/voice-test/whisper.env
```

Check variable exists without printing secret value:

```bash
awk -F= '$1=="WHISPER_API_KEY"{print "WHISPER_API_KEY is set"}' ~/.config/voice-test/whisper.env
```

### 9.7 TTS returns 401 Unauthorized

Likely cause:

```text
TTS_ROUTER_API_KEY missing or wrong in ~/.config/voice-test/whisper.env
```

Check variable exists without printing secret value:

```bash
awk -F= '$1=="TTS_ROUTER_API_KEY"{print "TTS_ROUTER_API_KEY is set"}' ~/.config/voice-test/whisper.env
```

### 9.8 TTS WAV file exists but no audible voice

Check these in order:

```bash
file ~/audio-test/kokoro-test-nonstream.wav
amixer -c 0
amixer -c 0 sset PCM 100% unmute
aplay -D plughw:0,0 ~/audio-test/kokoro-test-nonstream.wav
```

### 9.9 Router health/debug commands

```bash
curl -sS --max-time 10 http://192.168.1.21:11434/health
curl -sS --max-time 10 http://192.168.1.21:11434/api/ps
curl -sS --max-time 10 http://192.168.1.21:11434/api/version
```

Admin portal:

```text
http://192.168.1.21:11435/
```

---

## 10. Security and Operational Rules

### 10.1 Secrets

Secrets are stored only on the assistant machine in:

```text
~/.config/voice-test/whisper.env
```

Permissions should be:

```text
-rw-------
```

Do not paste keys into chat. Do not commit the config file.

### 10.2 Network exposure

This stack is designed for trusted LAN use.

Do not expose these directly to the public internet:

```text
192.168.1.21:11434
192.168.1.21:11435
192.168.1.22:8000
192.168.1.22:9000
```

### 10.3 Model management boundary

The assistant must not:

- Select a model
- Send a model name
- Load a model
- Swap a model
- Prewarm a model
- Modify active model state
- Depend on OpenWebUI for chat

The Ollama router/deployment stack controls the active model.

### 10.4 Router contract

The assistant calls:

```text
POST http://192.168.1.21:11434/api/chat
```

with:

```json
{
  "stream": false,
  "messages": [
    { "role": "system", "content": "..." },
    { "role": "user", "content": "..." }
  ]
}
```

The assistant must not include:

```json
{ "model": "..." }
```

---

## 11. Implementation Blueprint for the Larger Assistant

The current Bash scripts prove the integration, but the production assistant should be a small service with a clean state machine.

### 11.1 Recommended runtime responsibilities

The assistant service should handle:

- Audio input device discovery and selection
- Audio output device discovery and selection
- Mixer volume enforcement for the selected speakerphone
- Wake word detection
- Voice activity detection / end-of-speech detection
- STT request/response handling
- LLM router request/response handling
- TTS request/response handling
- Audio playback
- Logs and event history
- Configurable endpoints and voices
- Health/status UI

### 11.2 Recommended state machine

```text
IDLE
  -> WAKE_DETECTED
  -> LISTENING
  -> TRANSCRIBING
  -> THINKING
  -> SPEAKING
  -> IDLE
```

### 11.3 Suggested service boundaries

```text
voice-agent
  - audio capture
  - wake word
  - VAD
  - workflow orchestration
  - speaker playback

control-api / web portal
  - status
  - configuration
  - logs
  - health checks
  - endpoint testing
```

These can initially be one process/container, but should remain separated conceptually.

### 11.4 Recommended configuration fields

Assistant-local config should include:

```text
AUDIO_CAPTURE_DEVICE=plughw:0,0
AUDIO_PLAYBACK_DEVICE=plughw:0,0
AUDIO_CARD_INDEX=0
STT_URL=http://192.168.1.22:9000/v1/audio/transcriptions
TTS_URL=http://192.168.1.22:8000/v1/audio/speech
LLM_URL=http://192.168.1.21:11434/api/chat
TTS_MODEL=kokoro
TTS_VOICE=af_heart
TTS_RESPONSE_FORMAT=wav
```

Secrets should be separate or protected:

```text
WHISPER_API_KEY
TTS_ROUTER_API_KEY
```

### 11.5 Health checks the app should expose

The assistant app should display/pass/fail:

```text
USB speakerphone present
ALSA capture device available
ALSA playback device available
Mixer volume not zero
Whisper endpoint reachable
Whisper auth valid
TTS router reachable
TTS auth valid
Ollama router reachable
Ollama router active model present
Full loop last successful timestamp
```

### 11.6 Handling router failures

If the router fails, returns no active model, or returns malformed LLM output, the assistant should fail closed.

Preferred user-facing behavior:

```text
Use Kokoro to say a short error such as:
"The language model is not available right now."
```

Do not fall back to naming/loading a model from the assistant.

### 11.7 Future wake-word layer

Not yet implemented in these scripts.

Candidates:

- openWakeWord
- MicroWakeWord
- Home Assistant Wyoming-compatible wake-word service if the assistant later aligns with that ecosystem

The wake-word layer should trigger the already-validated chain:

```text
wake word -> record utterance -> STT -> LLM router -> TTS -> playback
```

---

## 12. Current Validation Status

| Component | Status | Notes |
|---|---:|---|
| EMEET USB detection | Validated | `lsusb`, `/proc/asound/cards`, ALSA card 0 |
| EMEET microphone capture | Validated | 16 kHz mono WAV captured successfully |
| EMEET speaker playback | Validated | Tone, recorded voice, and Kokoro playback confirmed |
| User audio permissions | Validated | User added to `audio` group |
| Whisper STT | Validated | Network transcription works with API key |
| Kokoro TTS | Validated | Router generates WAV and EMEET plays it |
| STT -> TTS loop | Validated | Transcript repeated by Kokoro |
| Old LLM gateway `8001` | Obsolete | No longer reachable in current deployment |
| Ollama router API `11434` | Validated | `/api/chat` works without client model field |
| Ollama router admin `11435` | Validated | Admin portal/status only, not API |
| Full STT -> LLM -> TTS loop | Validated | `listen-ask-speak` works again through router |
| OpenWebUI dependency | Excluded | Not part of assistant path |

---

## 13. Final Current Architecture

```text
Assistant machine: local-ai-assistant-1

  EMEET microphone
    -> arecord / future audio capture service
    -> Whisper STT
         http://192.168.1.22:9000/v1/audio/transcriptions
    -> Ollama router
         http://192.168.1.21:11434/api/chat
         no model field sent by assistant
    -> Kokoro TTS router
         http://192.168.1.22:8000/v1/audio/speech
         model=kokoro, voice=af_heart
    -> aplay / future audio playback service
    -> EMEET speaker
```

This is now the validated baseline for the larger assistant build.

