#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  ./tools/generate_tts_sounds.sh KOKORO_VOICE "phrase one" "phrase two" ...

Example:
  ./tools/generate_tts_sounds.sh af_heart "Yes?" "I'm listening." "Done."

Environment:
  FORCE=1        Overwrite existing generated files.
  PREVIEW=1      Play each generated file through the configured playback device.
  TTS_URL=...    Optional override. Defaults to services.tts.url from data/config.json.
  TTS_MODEL=...  Optional override. Defaults to services.tts.model from data/config.json or kokoro.

Behavior:
  - Loads TTS_ROUTER_API_KEY from .env.
  - Reads TTS URL/model/response format from data/config.json when available.
  - Generates WAV files through the Kokoro-compatible TTS endpoint.
  - Saves files into the configured sounds.library_dir, usually assets/sounds.
  - Uses the spoken phrase as the filename, lowercased and stripped of punctuation.
  - Installs files with the same numeric owner/group as the sounds directory.
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" || $# -lt 2 ]]; then
  usage
  exit 0
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ -f ".env" ]]; then
  set -a
  # shellcheck disable=SC1091
  . ".env"
  set +a
fi

if [[ -z "${TTS_ROUTER_API_KEY:-}" ]]; then
  echo "ERROR: TTS_ROUTER_API_KEY is not set. Put it in .env or export it before running." >&2
  exit 1
fi

VOICE="$1"
shift

CONFIG_EXPORTS="$(
python3 - <<'PY'
import json
import os
import shlex
from pathlib import Path

config_path = Path("data/config.json")
data = {}
if config_path.exists():
    data = json.loads(config_path.read_text(encoding="utf-8"))

tts = data.get("services", {}).get("tts", {})
sounds = data.get("sounds", {})

values = {
    "TTS_URL_EFFECTIVE": os.environ.get("TTS_URL") or tts.get("url") or "http://192.168.1.22:8000/v1/audio/speech",
    "TTS_MODEL_EFFECTIVE": os.environ.get("TTS_MODEL") or tts.get("model") or "kokoro",
    "TTS_RESPONSE_FORMAT_EFFECTIVE": os.environ.get("TTS_RESPONSE_FORMAT") or tts.get("response_format") or "wav",
    "SOUNDS_DIR_EFFECTIVE": sounds.get("library_dir") or "assets/sounds",
    "PLAYBACK_DEVICE_EFFECTIVE": data.get("audio", {}).get("playback_device") or "plughw:0,0",
}

for key, value in values.items():
    print(f"{key}={shlex.quote(str(value))}")
PY
)"

eval "$CONFIG_EXPORTS"

if [[ ! -d "$SOUNDS_DIR_EFFECTIVE" ]]; then
  echo "ERROR: sounds directory does not exist: $SOUNDS_DIR_EFFECTIVE" >&2
  exit 1
fi

SOUNDS_OWNER_GROUP="$(stat -c '%u:%g' "$SOUNDS_DIR_EFFECTIVE")"

sanitize_phrase() {
  python3 - "$1" <<'PY'
import re
import sys
import unicodedata

phrase = sys.argv[1].strip()
normalized = unicodedata.normalize("NFKD", phrase).encode("ascii", "ignore").decode("ascii")
normalized = normalized.lower()
normalized = normalized.replace("&", " and ")
normalized = re.sub(r"['’`]", "", normalized)
normalized = re.sub(r"[^a-z0-9]+", "_", normalized)
normalized = re.sub(r"_+", "_", normalized).strip("_")
print(normalized or "sound")
PY
}

make_request_json() {
  local phrase="$1"
  local request_file="$2"

  python3 - "$VOICE" "$phrase" "$TTS_MODEL_EFFECTIVE" "$TTS_RESPONSE_FORMAT_EFFECTIVE" > "$request_file" <<'PY'
import json
import sys

voice, phrase, model, response_format = sys.argv[1:5]

payload = {
    "model": model,
    "voice": voice,
    "response_format": response_format,
    "input": phrase,
}

json.dump(payload, sys.stdout)
PY
}

verify_wav() {
  local wav_file="$1"

  python3 - "$wav_file" <<'PY'
import sys
import wave

path = sys.argv[1]
with wave.open(path, "rb") as wav:
    channels = wav.getnchannels()
    sample_width = wav.getsampwidth()
    sample_rate = wav.getframerate()
    frames = wav.getnframes()
    duration = frames / sample_rate if sample_rate else 0

print(f"valid_wav=yes channels={channels} sample_width={sample_width} sample_rate={sample_rate} frames={frames} duration_seconds={duration:.3f}")
PY
}

echo "TTS URL: $TTS_URL_EFFECTIVE"
echo "TTS model: $TTS_MODEL_EFFECTIVE"
echo "Kokoro voice: $VOICE"
echo "Sounds dir: $SOUNDS_DIR_EFFECTIVE"
echo "Sounds owner: $SOUNDS_OWNER_GROUP"
echo

for PHRASE in "$@"; do
  BASE_NAME="$(sanitize_phrase "$PHRASE")"
  FINAL_NAME="${BASE_NAME}.wav"
  FINAL_PATH="${SOUNDS_DIR_EFFECTIVE%/}/${FINAL_NAME}"

  if [[ -e "$FINAL_PATH" && "${FORCE:-0}" != "1" ]]; then
    echo "SKIP: $FINAL_NAME already exists. Use FORCE=1 to overwrite."
    continue
  fi

  TMP_WAV="$(mktemp /tmp/voice-sound-XXXXXX.wav)"
  TMP_JSON="$(mktemp /tmp/voice-sound-request-XXXXXX.json)"

  cleanup() {
    rm -f "$TMP_WAV" "$TMP_JSON"
  }
  trap cleanup RETURN

  echo "Generating:"
  echo "  phrase: $PHRASE"
  echo "  file:   $FINAL_NAME"

  make_request_json "$PHRASE" "$TMP_JSON"

  curl -fS -X POST "$TTS_URL_EFFECTIVE" \
    -H "authorization: Bearer ${TTS_ROUTER_API_KEY}" \
    -H "content-type: application/json" \
    -o "$TMP_WAV" \
    --data-binary "@$TMP_JSON"

  echo "  verifying temp WAV..."
  verify_wav "$TMP_WAV"

  echo "  installing..."
  sudo cp "$TMP_WAV" "$FINAL_PATH"
  sudo chown "$SOUNDS_OWNER_GROUP" "$FINAL_PATH"
  sudo chmod 0644 "$FINAL_PATH"

  echo "  installed: $FINAL_PATH"
  ls -lh "$FINAL_PATH"

  if [[ "${PREVIEW:-0}" == "1" ]]; then
    echo "  previewing through $PLAYBACK_DEVICE_EFFECTIVE..."
    aplay -D "$PLAYBACK_DEVICE_EFFECTIVE" "$FINAL_PATH"
  fi

  echo
done