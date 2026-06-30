# Architecture

## Components

```text
wake.py                local wake-word sources and subprocess supervision
pocketsphinx_wake.py   local PocketSphinx stdout adapter for production wake
assistant.py           state machine and orchestration
commands.py            local command registry and recognizer adapters
audio.py               ALSA capture/playback, VAD, sound serialization
clients.py             Whisper STT, Ollama router LLM, Kokoro TTS clients
conversation.py        local conversation context lifecycle
telemetry.py           SQLite events and artifacts
app.py                 admin portal and local-network API
maintenance.py         cleanup, restart, reboot actions
health.py              component/service health checks
```

## State model

The runtime exposes human-readable states rather than requiring callers to know implementation enum names:

```text
waiting for the wake word
wake word detected
capturing a prompt
checking for a local command
processing STT
processing LLM
processing TTS
playing a response
handling an error
```

The state machine is deliberately conservative: local command recognition always runs before main STT, and the LLM/TTS pipeline runs only when the main STT service returns non-empty text.

## Audio cue sequencing

The audio controller serializes short local sound effects. The wake acknowledgement sound and prompt capture are scheduled together so the user can begin speaking as the sound starts. The runtime pauses wake listening while prompt capture owns ALSA and resumes it immediately after capture, allowing barge-in during STT, LLM, TTS, and playback without fighting the prompt recorder for the microphone. The thinking loop is stopped before prompt-accepted, failure, or TTS playback sounds to avoid confusing overlap. Barge-in cancellation calls `stop_all_playback()` and cancels the active task.

## Conversation context

The thin client keeps the raw message list until one of these happens:

- configured inactivity timeout expires after response playback completion;
- local `new_conversation` command is accepted.

The thin client does not summarize or truncate messages. Any model context-window handling remains a backend concern.

## Persistence

Configuration is JSON. Telemetry and artifacts use SQLite plus filesystem WAV storage.

```text
data/config.json             saved configuration
data/config.draft.json       grouped apply draft, when present
data/telemetry.sqlite3       event and artifact index
data/artifacts/              prompt, TTS, and admin microphone WAV artifacts
assets/sounds/               local sound effects
```

## Restart-required configuration

The config store classifies `services.stt`, `services.llm`, and `services.tts` paths as restart-required. Applying those settings persists them to the saved config, but the active runtime values remain unchanged until the service restarts. Other settings are applied live.

## Wake engine boundary

Wake engines are local-only. The downstream Whisper service is never used as a wake detector.

The package includes three engines:

- `simulated`: admin/test event source.
- `openwakeword`: feeds ALSA microphone chunks into openWakeWord.
- `external_command`: runs a local dedicated wake process and treats stdout detections as wake events.

The production default is `external_command` with `python -m voice_assistant.pocketsphinx_wake`, which wraps `pocketsphinx_continuous` and emits JSON detections such as:

```json
{"event":"wake","engine":"pocketsphinx_continuous","phrase":"computer","confidence":null}
```

The external adapter supervises the process, restarts it if it exits unexpectedly, terminates it cleanly on shutdown, and exposes process/detection status through `/api/status` and `/api/wake/debug`.

## Command recognition boundary

The command recognizer runs only after wake detection and prompt capture. It is not a continuous general-purpose listener. It evaluates the whole utterance against the configured alias registry.

The optional Vosk recognizer can be used to locally transcribe short command audio before main STT. Tests use the configured text recognizer with sidecar transcripts so no network STT is involved.

## Error handling

Downstream failures stop the thinking loop, log telemetry, play the configured failure sound, and return to idle without clearing conversation context. STT no-text is treated as an invalid prompt and does not invoke the LLM.
