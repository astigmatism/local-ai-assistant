from __future__ import annotations

import os
import re
import unicodedata
import uuid
import wave
from pathlib import Path
from typing import Any, Callable, Iterable

from .config import AssistantConfig

# Static Kokoro-82M voice list. The project has no router voice-discovery endpoint today, so the
# admin endpoint exposes this maintainable Kokoro-only list and validates selections against it.
KOKORO_VOICE_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "American English",
        (
            "af_heart",
            "af_alloy",
            "af_aoede",
            "af_bella",
            "af_jessica",
            "af_kore",
            "af_nicole",
            "af_nova",
            "af_river",
            "af_sarah",
            "af_sky",
            "am_adam",
            "am_echo",
            "am_eric",
            "am_fenrir",
            "am_liam",
            "am_michael",
            "am_onyx",
            "am_puck",
            "am_santa",
        ),
    ),
    ("British English", ("bf_alice", "bf_emma", "bf_isabella", "bf_lily", "bm_daniel", "bm_fable", "bm_george", "bm_lewis")),
    ("Japanese", ("jf_alpha", "jf_gongitsune", "jf_nezumi", "jf_tebukuro", "jm_kumo")),
    ("Mandarin Chinese", ("zf_xiaobei", "zf_xiaoni", "zf_xiaoxiao", "zf_xiaoyi", "zm_yunjian", "zm_yunxi", "zm_yunxia", "zm_yunyang")),
    ("Spanish", ("ef_dora", "em_alex", "em_santa")),
    ("French", ("ff_siwis",)),
    ("Hindi", ("hf_alpha", "hf_beta", "hm_omega", "hm_psi")),
    ("Italian", ("if_sara", "im_nicola")),
    ("Brazilian Portuguese", ("pf_dora", "pm_alex", "pm_santa")),
)

KOKORO_VOICES: tuple[str, ...] = tuple(voice for _language, voices in KOKORO_VOICE_GROUPS for voice in voices)
KOKORO_VOICE_SET: frozenset[str] = frozenset(KOKORO_VOICES)


def kokoro_voice_options() -> list[dict[str, str]]:
    return [
        {"id": voice, "voice": voice, "language": language, "label": f"{voice} ({language})"}
        for language, voices in KOKORO_VOICE_GROUPS
        for voice in voices
    ]


def validate_kokoro_voice(voice: str) -> str:
    normalized = (voice or "").strip()
    if normalized not in KOKORO_VOICE_SET:
        raise ValueError(f"Unsupported Kokoro voice: {normalized or '<empty>'}")
    return normalized


def config_with_tts_voice(config: AssistantConfig, voice: str) -> AssistantConfig:
    voice = validate_kokoro_voice(voice)
    data = config.public_dict()
    data["services"]["tts"]["voice"] = voice
    return AssistantConfig.model_validate(data)


def sanitize_tts_sound_phrase(phrase: str) -> str:
    normalized = unicodedata.normalize("NFKD", phrase.strip()).encode("ascii", "ignore").decode("ascii")
    normalized = normalized.lower()
    normalized = normalized.replace("&", " and ")
    normalized = re.sub(r"['’`]", "", normalized)
    normalized = re.sub(r"[^a-z0-9]+", "_", normalized)
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    return normalized or "sound"


def phrase_output_filename(phrase: str) -> str:
    return f"{sanitize_tts_sound_phrase(phrase)}.wav"


def normalize_generated_tts_phrases(phrases: Iterable[str]) -> list[str]:
    cleaned: list[str] = []
    output_names: dict[str, str] = {}
    for raw_phrase in phrases:
        phrase = (raw_phrase or "").strip()
        if not phrase:
            continue
        filename = phrase_output_filename(phrase)
        existing_phrase = output_names.get(filename)
        if existing_phrase is not None:
            raise ValueError(
                f"Generated TTS phrases {existing_phrase!r} and {phrase!r} both target {filename!r}"
            )
        output_names[filename] = phrase
        cleaned.append(phrase)
    if not cleaned:
        raise ValueError("Generated TTS sound phrases must include at least one phrase")
    return cleaned


def _validate_wav(path: Path) -> dict[str, Any]:
    with wave.open(str(path), "rb") as wav:
        channels = wav.getnchannels()
        sample_width = wav.getsampwidth()
        sample_rate = wav.getframerate()
        frames = wav.getnframes()
    if channels <= 0 or sample_width <= 0 or sample_rate <= 0 or frames <= 0:
        raise ValueError(f"Generated audio is not a usable WAV file: {path.name}")
    return {
        "channels": channels,
        "sample_width": sample_width,
        "sample_rate": sample_rate,
        "frames": frames,
        "duration_seconds": frames / sample_rate,
    }


def _verify_sound_library_writable(sound_dir: Path) -> None:
    try:
        sound_dir.mkdir(parents=True, exist_ok=True)
        probe_path = sound_dir / f".write-test.{uuid.uuid4().hex}.tmp"
        with probe_path.open("xb"):
            pass
        probe_path.unlink()
    except PermissionError as exc:
        raise PermissionError(
            "The configured sound library directory is not writable by the assistant runtime: "
            f"{sound_dir}. Ensure the host bind mount is writable by the container user before regenerating sounds."
        ) from exc
    except OSError as exc:
        raise OSError(
            "The configured sound library directory cannot be used for generated sounds: "
            f"{sound_dir}: {exc}"
        ) from exc


async def regenerate_generated_tts_sounds(
    config: AssistantConfig,
    tts_factory: Callable[[AssistantConfig], Any],
    *,
    voice: str,
    phrases: Iterable[str] | None = None,
) -> dict[str, Any]:
    effective_config = config_with_tts_voice(config, voice)
    phrase_list = normalize_generated_tts_phrases(phrases if phrases is not None else effective_config.sounds.generated_tts_phrases)

    sound_dir = Path(effective_config.sounds.library_dir)
    _verify_sound_library_writable(sound_dir)
    owner_group = sound_dir.stat()
    generated: list[dict[str, Any]] = []
    touched: set[str] = set()

    for phrase in phrase_list:
        final_name = phrase_output_filename(phrase)
        if final_name in touched:
            raise ValueError(f"Generated TTS sound phrase list contains duplicate output file: {final_name}")
        touched.add(final_name)
        final_path = sound_dir / final_name
        tmp_path = sound_dir / f".{final_name}.{uuid.uuid4().hex}.tmp.wav"
        try:
            await tts_factory(effective_config).synthesize(phrase, tmp_path)
            wav_details = _validate_wav(tmp_path)
            tmp_path.replace(final_path)
            os.chmod(final_path, 0o644)
            current_owner = final_path.stat()
            if (current_owner.st_uid, current_owner.st_gid) != (owner_group.st_uid, owner_group.st_gid):
                os.chown(final_path, owner_group.st_uid, owner_group.st_gid)
            generated.append(
                {
                    "phrase": phrase,
                    "filename": final_name,
                    "path": str(final_path),
                    "overwritten": True,
                    "wav": wav_details,
                }
            )
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

    return {
        "voice": effective_config.services.tts.voice,
        "sound_directory": str(sound_dir),
        "phrases": phrase_list,
        "generated_files": generated,
        "generated_count": len(generated),
    }
