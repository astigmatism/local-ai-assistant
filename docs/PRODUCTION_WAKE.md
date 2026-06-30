# Production wake-word deployment: PocketSphinx external command

## Selected approach

This package uses the existing `external_command` wake adapter as the production path, with a source-controlled command module:

```text
python -m voice_assistant.pocketsphinx_wake
```

The command wraps the local `pocketsphinx_continuous` keyword spotter, but it does **not** use PocketSphinx's `-inmic yes` live microphone backend. The target thin client proved that `arecord -D plughw:0,0` can open the EMEET microphone while `pocketsphinx_continuous -inmic yes -adcdev plughw:0,0` exits with `Connection refused`.

The packaged command now keeps one local continuous `arecord` raw PCM capture open, stores only a rolling in-memory pre-wake buffer, and decodes overlapping recent windows:

1. Capture raw 16 kHz mono PCM from `plughw:0,0` with `arecord`.
2. Keep the most recent decode window in memory.
3. Every hop interval, send the recent finite window to `pocketsphinx_continuous -infile /dev/stdin`.
4. Parse the decoder stdout after EOF for that window.
5. Emit one JSON wake event when `Rosalina` is detected.
6. Apply a short cooldown and continue capturing.

Default timing is a 4.0 second decode window with a 1.0 second hop, so each decode has 3.0 seconds of overlap with the previous decode. This preserves the target hardware behavior where PocketSphinx receives EOF for each decoded window, while reducing wake-word misses at fixed window boundaries.

The main assistant supervises that wrapper subprocess, parses the JSON event, pauses wake listening, plays the wake acknowledgement to completion, captures the prompt, and then resumes wake listening for barge-in during STT/LLM/TTS/playback. The wrapper cleans up active `arecord` and `pocketsphinx_continuous` children when it receives SIGTERM/SIGINT.

This approach was selected because it avoids the failed `openwakeword` optional dependency path under the current Python 3.12 image, keeps wake detection local, uses Debian packages installed at image build time, preserves the existing `external_command` abstraction for future wake engines, and improves reliability over non-overlapping finite chunks.

## Changed behavior

Fresh deployments default to:

```json
{
  "wake": {
    "engine": "external_command",
    "wake_phrases": ["Rosalina"],
    "active_wake_phrase": "Rosalina",
    "external_command": ["python", "-m", "voice_assistant.pocketsphinx_wake"],
    "external_health_command": ["python", "-m", "voice_assistant.pocketsphinx_wake", "--self-test"],
    "sensitivity": 0.5
  }
}
```

The packaged wrapper defaults are tuned for the target EMEET deployment:

```text
capture device: plughw:0,0
phrase: Rosalina
sample rate: 16000 Hz
channels: 1
decode window: 4.0 seconds
hop interval: 1.0 second
overlap: 3.0 seconds
threshold: 1e-20 when wake.sensitivity is 0.5
cooldown after detection: 1.5 seconds
backend: pocketsphinx_continuous_arecord_overlap
```

The old packaged default phrase was `computer`. Persisted configs that still use that old default are automatically upgraded to `Rosalina` on load, and the explicit production-wake migration endpoint also sets `Rosalina` as the active production phrase. The old phrase may appear only in historical notes or migration tests.

The `simulated` engine and `POST /api/test/wake` remain available, but they are labeled as admin/test diagnostics and are not represented as the production input method.

## Pronunciation and dictionary handling

The wrapper checks the PocketSphinx dictionary selected by `--dict` / `VOICE_ASSISTANT_POCKETSPHINX_DICT` before it starts decoding. Proper names are not guaranteed to be present in the Debian `pocketsphinx-en-us` dictionary, so the wrapper does not silently rely on an unknown word.

When `rosalina` is absent from the base dictionary, the wrapper creates a deterministic merged runtime dictionary at:

```text
/tmp/voice-assistant-pocketsphinx/wake-custom.dict
```

The generated entries are:

```text
rosalina R OW Z AH L IY N AH
rosalina(2) R OW S AH L IY N AH
```

The primary CMU-style pronunciation represents the common English “roh-zuh-LEE-nuh” pronunciation. The second entry accepts a less-voiced spelling pronunciation for the `s`. The base system dictionary is not edited in place, and the custom path can be overridden with `--custom-dict-path` or `VOICE_ASSISTANT_POCKETSPHINX_CUSTOM_DICT`.

## Build and deploy on the thin client

From the repository checkout on `local-ai-assistant-1`:

```bash
cd ~/apps/local-voice-assistant
cp -n .env.example .env
chmod 600 .env
# edit .env and set WHISPER_API_KEY and TTS_ROUTER_API_KEY

docker compose build voice-assistant
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

## Upgrade/migrate an existing deployment

Existing deployments may have a persisted `data/config.json`; rebuilding the image does not overwrite that file. If the saved config still has the old `computer` default, the app upgrades it to `Rosalina` when loading config. If the saved config still has `wake.engine = simulated`, use the admin API migration endpoint after deploying the new image:

```bash
curl -sS -X POST http://192.168.1.23:8080/api/config/migrate-production-wake \
  -H 'content-type: application/json' \
  -d '{"confirm":true}'
```

The migration updates only wake source fields and the active production phrase, preserves unrelated saved settings, reloads the runtime wake listener, and writes the updated saved config back to `data/config.json`.

Equivalent result in `data/config.json`:

```json
{
  "wake": {
    "engine": "external_command",
    "wake_phrases": ["Rosalina"],
    "active_wake_phrase": "Rosalina",
    "external_command": ["python", "-m", "voice_assistant.pocketsphinx_wake"],
    "external_health_command": ["python", "-m", "voice_assistant.pocketsphinx_wake", "--self-test"]
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
- `/api/status` shows `wake.active_wake_phrase: Rosalina`.
- `/api/status` shows `wake.packaged_backend: pocketsphinx_continuous_arecord_overlap` and `wake.capture_backend: arecord_stream_overlap`.
- `/api/status` shows `wake.window_seconds: 4.0`, `wake.hop_seconds: 1.0`, `wake.overlap_seconds: 3.0`, and the current threshold.
- `/api/status` shows `wake.process_running: true` after startup and after the migration reload completes.
- `/api/health` includes passing `wake-word engine` and `wake-word runtime` checks.
- `/api/wake/debug` shows real wake detections, recent stderr diagnostics if any, and separately labels `/api/test/wake` as simulated/admin-only.

Voice-only hardware validation:

1. Say `Rosalina` near the EMEET speakerphone.
2. Hear the full wake acknowledgement sound.
3. Ask a simple question after the acknowledgement finishes, for example: “What is two plus two?”
4. Hear the spoken answer through the EMEET speakerphone.
5. Confirm telemetry contains this sequence: `wake_detected -> wake_ack_playback_started -> wake_ack_playback_ended -> prompt_capture_started -> prompt_capture_ended -> command_recognition_started -> stt_started -> llm_started -> tts_started -> playback_started -> playback_ended`.
6. Reboot the thin client.
7. Repeat the same voice-only test without opening the admin portal, pressing a key, running SSH commands, or posting to `/api/test/wake`.

## Audio/mixer note

The app still enforces the configured PCM mixer volume before playback. The known-good host boot workaround remains useful if the USB speakerphone resets volume after reboot or movement:

```bash
amixer -c 0 sset PCM 100% unmute
```

Keep `audio.enforce_pcm_volume_percent = 100` and `audio.mixer_card_index = 0` in app config for the current EMEET deployment.

## Privacy boundary

Pre-wake audio is consumed only by the local PocketSphinx wake subprocess. The rolling pre-wake buffer is in memory only, and it is not sent to Whisper, the LLM router, TTS, cloud services, or telemetry/artifact storage. Post-wake prompt audio continues to follow the existing telemetry and artifact-retention settings.

## Tuning

The app-level `wake.sensitivity` remains a 0.0 to 1.0 value. The wrapper maps it to PocketSphinx's keyword threshold. Higher values are more permissive. At the default sensitivity of `0.5`, the threshold is `1e-20`.

The wrapper can also be tuned with CLI flags or environment variables without changing Python source:

```text
--phrase / VOICE_ASSISTANT_WAKE_PHRASE
--device / VOICE_ASSISTANT_CAPTURE_DEVICE
--sample-rate / VOICE_ASSISTANT_SAMPLE_RATE_HZ
--channels / VOICE_ASSISTANT_CHANNELS
--window-seconds / VOICE_ASSISTANT_WAKE_WINDOW_SECONDS
--hop-seconds / VOICE_ASSISTANT_WAKE_HOP_SECONDS
--cooldown-seconds / VOICE_ASSISTANT_WAKE_COOLDOWN_SECONDS
--threshold / VOICE_ASSISTANT_POCKETSPHINX_THRESHOLD
--hmm / VOICE_ASSISTANT_WAKE_MODEL_PATH
--dict / VOICE_ASSISTANT_POCKETSPHINX_DICT
--custom-dict-path / VOICE_ASSISTANT_POCKETSPHINX_CUSTOM_DICT
--max-chunks
--diagnostic-summary
```

`--chunk-seconds` and `VOICE_ASSISTANT_WAKE_CHUNK_SECONDS` remain accepted as backward-compatible aliases for the decode window length. New tuning should use `--window-seconds` / `VOICE_ASSISTANT_WAKE_WINDOW_SECONDS`.

For the packaged production command, the app sets phrase, sensitivity, device, sample rate, and channel count from active config through environment variables when it starts the external wake process.

For advanced troubleshooting, run the wrapper self-test inside the container:

```bash
docker compose exec voice-assistant python -m voice_assistant.pocketsphinx_wake --self-test
```

The self-test output should include:

```json
{
  "engine": "pocketsphinx_continuous_arecord_overlap",
  "capture_backend": "arecord_stream_overlap",
  "phrase": "Rosalina",
  "window_seconds": 4.0,
  "hop_seconds": 1.0,
  "overlap_seconds": 3.0,
  "cooldown_seconds": 1.5
}
```

For an interactive reliability check where you say `Rosalina` several times and get a final summary, run:

```bash
docker compose exec voice-assistant python -m voice_assistant.pocketsphinx_wake \
  --phrase Rosalina \
  --max-chunks 10 \
  --diagnostic-summary
```

The final `diagnostic_summary` JSON reports `chunks_processed`, `detections`, `last_detection_phrase`, backend, threshold, window, hop, overlap, dictionary path, and custom pronunciation entries when they were needed.
