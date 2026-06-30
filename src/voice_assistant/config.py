from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from .constants import CommandIntent, SoundEvent


RESTART_REQUIRED_PREFIXES = {
    "services.stt",
    "services.llm",
    "services.tts",
}


class WakeConfig(BaseModel):
    engine: Literal["simulated", "openwakeword", "external_command"] = "simulated"
    wake_phrases: list[str] = Field(default_factory=lambda: ["computer"])
    active_wake_phrase: str = "computer"
    model_path: str | None = None
    sensitivity: float = Field(0.5, ge=0.0, le=1.0)
    external_command: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def active_phrase_must_exist(self) -> "WakeConfig":
        if self.active_wake_phrase not in self.wake_phrases:
            raise ValueError("active_wake_phrase must be present in wake_phrases")
        return self


class PromptCaptureConfig(BaseModel):
    minimum_duration_seconds: float = Field(3.0, ge=0.0)
    maximum_duration_seconds: float = Field(120.0, gt=0.0)
    silence_duration_seconds: float = Field(1.0, ge=0.1)
    silence_rms_threshold: int = Field(500, ge=0)
    chunk_milliseconds: int = Field(50, ge=10, le=500)

    @model_validator(mode="after")
    def maximum_not_less_than_minimum(self) -> "PromptCaptureConfig":
        if self.maximum_duration_seconds < self.minimum_duration_seconds:
            raise ValueError("maximum_duration_seconds must be >= minimum_duration_seconds")
        return self


class ConversationConfig(BaseModel):
    inactivity_timeout_seconds: float = Field(60.0, gt=0.0)
    system_prompt: str = (
        "You are a concise voice assistant. Answer naturally in one or two short sentences. "
        "Do not mention implementation details."
    )


class CommandDefinition(BaseModel):
    intent: CommandIntent
    aliases: list[str]
    behavior: str
    acknowledgement_sound_event: SoundEvent
    enabled: bool = True

    @field_validator("aliases")
    @classmethod
    def aliases_non_empty(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item and item.strip()]
        if not cleaned:
            raise ValueError("command aliases must not be empty")
        return cleaned


class CommandRecognizerConfig(BaseModel):
    engine: Literal["configured_text", "vosk"] = "configured_text"
    vosk_model_path: str | None = None
    confidence_threshold: float = Field(0.70, ge=0.0, le=1.0)


class CommandRegistryConfig(BaseModel):
    recognizer: CommandRecognizerConfig = Field(default_factory=CommandRecognizerConfig)
    commands: list[CommandDefinition] = Field(
        default_factory=lambda: [
            CommandDefinition(
                intent=CommandIntent.CANCEL_STOP,
                aliases=["stop", "cancel", "never mind", "forget it"],
                behavior="cancel_active_process_and_return_idle_preserve_context",
                acknowledgement_sound_event=SoundEvent.CANCEL_ACCEPTED,
            ),
            CommandDefinition(
                intent=CommandIntent.NEW_CONVERSATION,
                aliases=["new conversation", "start a new conversation", "start over", "new chat"],
                behavior="clear_context_and_start_new_prompt_capture",
                acknowledgement_sound_event=SoundEvent.NEW_CONVERSATION_ACCEPTED,
            ),
        ]
    )

    @model_validator(mode="after")
    def v1_intents_only_by_default(self) -> "CommandRegistryConfig":
        seen: set[CommandIntent] = set()
        for command in self.commands:
            if command.intent in seen:
                raise ValueError(f"duplicate command intent: {command.intent}")
            seen.add(command.intent)
        return self


class SoundConfig(BaseModel):
    library_dir: str = "assets/sounds"
    event_files: dict[SoundEvent, str] = Field(
        default_factory=lambda: {
            SoundEvent.WAKE_ACK: "wake_ack.wav",
            SoundEvent.INVALID_PROMPT: "failure.wav",
            SoundEvent.PROMPT_ACCEPTED: "prompt_accepted.wav",
            SoundEvent.THINKING: "thinking.wav",
            SoundEvent.CANCEL_ACCEPTED: "command_accepted.wav",
            SoundEvent.NEW_CONVERSATION_ACCEPTED: "new_conversation.wav",
            SoundEvent.STT_FAILURE: "failure.wav",
            SoundEvent.LLM_FAILURE: "failure.wav",
            SoundEvent.TTS_FAILURE: "failure.wav",
            SoundEvent.NETWORK_FAILURE: "failure.wav",
            SoundEvent.INTERNAL_FAILURE: "failure.wav",
            SoundEvent.ADMIN_TEST: "admin_test.wav",
        }
    )

    @model_validator(mode="after")
    def all_events_configurable(self) -> "SoundConfig":
        missing = [event for event in SoundEvent if event not in self.event_files]
        if missing:
            raise ValueError(f"missing sound event mappings: {missing}")
        return self


class TelemetryConfig(BaseModel):
    audio_artifact_storage_enabled: bool = True
    retention_days: int = Field(365, ge=1)
    cleanup_interval: Literal["daily"] = "daily"
    cleanup_time_of_day: str = "03:00"

    @field_validator("cleanup_time_of_day")
    @classmethod
    def validate_hhmm(cls, value: str) -> str:
        parts = value.split(":")
        if len(parts) != 2:
            raise ValueError("cleanup_time_of_day must be HH:MM")
        hour, minute = int(parts[0]), int(parts[1])
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("cleanup_time_of_day must be HH:MM")
        return value


class STTServiceConfig(BaseModel):
    url: str = "http://192.168.1.22:9000/v1/audio/transcriptions"
    model: str = "whisper-1"
    response_format: Literal["json", "text"] = "json"
    timeout_seconds: float = Field(60.0, gt=0)
    api_key_env: str = "WHISPER_API_KEY"


class LLMServiceConfig(BaseModel):
    url: str = "http://192.168.1.21:11434/api/chat"
    health_url: str = "http://192.168.1.21:11434/health"
    timeout_seconds: float = Field(180.0, gt=0)
    stream: bool = False


class TTSServiceConfig(BaseModel):
    url: str = "http://192.168.1.22:8000/v1/audio/speech"
    model: str = "kokoro"
    voice: str = "af_heart"
    response_format: Literal["wav"] = "wav"
    speed: float = Field(1.0, gt=0)
    volume_multiplier: float = Field(1.5, gt=0)
    timeout_seconds: float = Field(90.0, gt=0)
    api_key_env: str = "TTS_ROUTER_API_KEY"


class ServiceConfig(BaseModel):
    stt: STTServiceConfig = Field(default_factory=STTServiceConfig)
    llm: LLMServiceConfig = Field(default_factory=LLMServiceConfig)
    tts: TTSServiceConfig = Field(default_factory=TTSServiceConfig)


class AudioDeviceConfig(BaseModel):
    capture_device: str = "plughw:0,0"
    playback_device: str = "plughw:0,0"
    mixer_card_index: int = Field(0, ge=0)
    enforce_pcm_volume_percent: int = Field(100, ge=0, le=100)
    sample_rate_hz: int = 16000
    channels: int = 1


class StorageConfig(BaseModel):
    base_dir: str = "data"
    artifacts_dir: str = "data/artifacts"
    telemetry_db_path: str = "data/telemetry.sqlite3"


class MaintenanceConfig(BaseModel):
    host_command_execution_enabled: bool = False
    assistant_restart_command: list[str] = Field(default_factory=lambda: ["systemctl", "restart", "voice-assistant"])
    machine_reboot_command: list[str] = Field(default_factory=lambda: ["sudo", "/sbin/reboot"])


class AdminConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = Field(8080, ge=1, le=65535)


class AssistantConfig(BaseModel):
    wake: WakeConfig = Field(default_factory=WakeConfig)
    prompt_capture: PromptCaptureConfig = Field(default_factory=PromptCaptureConfig)
    conversation: ConversationConfig = Field(default_factory=ConversationConfig)
    command_registry: CommandRegistryConfig = Field(default_factory=CommandRegistryConfig)
    sounds: SoundConfig = Field(default_factory=SoundConfig)
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
    services: ServiceConfig = Field(default_factory=ServiceConfig)
    audio: AudioDeviceConfig = Field(default_factory=AudioDeviceConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    maintenance: MaintenanceConfig = Field(default_factory=MaintenanceConfig)
    admin: AdminConfig = Field(default_factory=AdminConfig)

    def public_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class ConfigApplyResult(BaseModel):
    active: dict[str, Any]
    saved: dict[str, Any]
    pending_restart_paths: list[str]
    applied_runtime_paths: list[str]


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _flatten_diff_paths(left: Any, right: Any, prefix: str = "") -> list[str]:
    if isinstance(left, dict) and isinstance(right, dict):
        paths: list[str] = []
        for key in sorted(set(left) | set(right)):
            new_prefix = f"{prefix}.{key}" if prefix else str(key)
            paths.extend(_flatten_diff_paths(left.get(key), right.get(key), new_prefix))
        return paths
    if left != right:
        return [prefix]
    return []


def _requires_restart(path: str) -> bool:
    return any(path == prefix or path.startswith(prefix + ".") for prefix in RESTART_REQUIRED_PREFIXES)


class ConfigStore:
    """Stores default, saved, active, and draft configuration.

    Saved values are persisted immediately on apply. Runtime-active values are updated for most
    settings, but STT/LLM/TTS connection settings remain pending until process restart.
    """

    def __init__(self, config_path: str | Path):
        self.config_path = Path(config_path)
        self.draft_path = self.config_path.with_suffix(".draft.json")
        self._lock = threading.RLock()
        self.defaults = AssistantConfig()
        self.saved = self._load_saved()
        self.active = copy.deepcopy(self.saved)
        self.pending_restart_paths: list[str] = []
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.config_path.exists():
            self._write_json(self.config_path, self.saved.public_dict())

    def _load_saved(self) -> AssistantConfig:
        if not self.config_path.exists():
            return AssistantConfig()
        with self.config_path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return AssistantConfig.model_validate(data)

    @staticmethod
    def _write_json(path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        tmp.replace(path)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            draft = None
            if self.draft_path.exists():
                with self.draft_path.open("r", encoding="utf-8") as handle:
                    draft = json.load(handle)
            return {
                "defaults": self.defaults.public_dict(),
                "saved": self.saved.public_dict(),
                "active": self.active.public_dict(),
                "draft": draft,
                "pending_restart_paths": list(self.pending_restart_paths),
                "restart_required_prefixes": sorted(RESTART_REQUIRED_PREFIXES),
            }

    def save_draft(self, patch: dict[str, Any]) -> AssistantConfig:
        with self._lock:
            merged = _deep_merge(self.saved.public_dict(), patch)
            draft = AssistantConfig.model_validate(merged)
            self._write_json(self.draft_path, draft.public_dict())
            return draft

    def clear_draft(self) -> None:
        with self._lock:
            if self.draft_path.exists():
                self.draft_path.unlink()

    def apply_draft(self) -> ConfigApplyResult:
        with self._lock:
            if not self.draft_path.exists():
                raise FileNotFoundError("no draft configuration has been saved")
            with self.draft_path.open("r", encoding="utf-8") as handle:
                draft_data = json.load(handle)
            return self.apply_config(draft_data, remove_draft=True)

    def import_to_draft(self, imported: dict[str, Any]) -> AssistantConfig:
        return self.save_draft(imported)

    def apply_config(self, new_config_data: dict[str, Any], remove_draft: bool = False) -> ConfigApplyResult:
        with self._lock:
            new_saved = AssistantConfig.model_validate(new_config_data)
            old_saved_dict = self.saved.public_dict()
            new_saved_dict = new_saved.public_dict()
            changed_paths = _flatten_diff_paths(old_saved_dict, new_saved_dict)
            restart_paths = [path for path in changed_paths if _requires_restart(path)]
            runtime_paths = [path for path in changed_paths if not _requires_restart(path)]

            active_dict = self.active.public_dict()
            for path in runtime_paths:
                self._copy_path(new_saved_dict, active_dict, path)
            self.active = AssistantConfig.model_validate(active_dict)
            self.saved = new_saved
            self.pending_restart_paths = sorted(set(self.pending_restart_paths + restart_paths))
            self._write_json(self.config_path, self.saved.public_dict())
            if remove_draft:
                self.clear_draft()
            return ConfigApplyResult(
                active=self.active.public_dict(),
                saved=self.saved.public_dict(),
                pending_restart_paths=list(self.pending_restart_paths),
                applied_runtime_paths=runtime_paths,
            )

    @staticmethod
    def _copy_path(src: dict[str, Any], dest: dict[str, Any], dotted: str) -> None:
        parts = dotted.split(".")
        src_cursor: Any = src
        dest_cursor: Any = dest
        for part in parts[:-1]:
            src_cursor = src_cursor[part]
            dest_cursor = dest_cursor.setdefault(part, {})
        dest_cursor[parts[-1]] = copy.deepcopy(src_cursor[parts[-1]])

    def mark_restart_completed(self) -> None:
        with self._lock:
            self.active = copy.deepcopy(self.saved)
            self.pending_restart_paths = []

    def get_active(self) -> AssistantConfig:
        with self._lock:
            return copy.deepcopy(self.active)

    def get_saved(self) -> AssistantConfig:
        with self._lock:
            return copy.deepcopy(self.saved)


def load_config_store_from_env() -> ConfigStore:
    config_path = os.getenv("VOICE_ASSISTANT_CONFIG", "data/config.json")
    return ConfigStore(config_path)


__all__ = [
    "AssistantConfig",
    "ConfigStore",
    "ConfigApplyResult",
    "ValidationError",
    "RESTART_REQUIRED_PREFIXES",
]
