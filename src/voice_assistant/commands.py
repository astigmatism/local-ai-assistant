from __future__ import annotations

import abc
import json
import re
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .config import CommandDefinition, CommandRegistryConfig


_WORD_RE = re.compile(r"[a-z0-9']+")


@dataclass(frozen=True)
class CommandMatch:
    intent: str
    alias: str
    transcript: str
    confidence: float = 1.0


class CommandRegistry:
    def __init__(self, config: CommandRegistryConfig):
        self.config = config
        self._commands = {command.intent.value: command for command in config.commands}

    def enabled_commands(self) -> list[CommandDefinition]:
        return [command for command in self.config.commands if command.enabled]

    def get(self, intent: str) -> CommandDefinition | None:
        return self._commands.get(intent)

    @staticmethod
    def normalize(text: str) -> str:
        words = _WORD_RE.findall(text.lower().strip())
        return " ".join(words)

    def match_text(self, transcript: str) -> CommandMatch | None:
        normalized = self.normalize(transcript)
        if not normalized:
            return None
        for command in self.enabled_commands():
            for alias in command.aliases:
                if normalized == self.normalize(alias):
                    return CommandMatch(
                        intent=command.intent.value,
                        alias=alias,
                        transcript=transcript,
                        confidence=1.0,
                    )
        return None


class LocalCommandRecognizer(abc.ABC):
    @abc.abstractmethod
    async def recognize(self, audio_path: str | Path, registry: CommandRegistry, hinted_text: str | None = None) -> CommandMatch | None:
        raise NotImplementedError


class ConfiguredTextCommandRecognizer(LocalCommandRecognizer):
    """Local recognizer for tests, diagnostics, and integrations that provide a local transcript.

    It never calls the main STT service. When no local text hint or sidecar exists, it returns no
    command and the regular STT pipeline can process the prompt.
    """

    async def recognize(self, audio_path: str | Path, registry: CommandRegistry, hinted_text: str | None = None) -> CommandMatch | None:
        text = hinted_text
        if text is None:
            sidecar = Path(str(audio_path) + ".command.txt")
            if sidecar.exists():
                text = sidecar.read_text(encoding="utf-8")
        if text is None:
            return None
        return registry.match_text(text)


class VoskCommandRecognizer(LocalCommandRecognizer):
    """Bounded, local command recognizer using a configured Vosk model.

    The recognizer transcribes only the post-wake captured utterance for matching against the small
    local command registry; it is not a continuous general-purpose command listener and it does not
    use the downstream/main STT service.
    """

    def __init__(self, model_path: str, confidence_threshold: float = 0.70):
        self.model_path = model_path
        self.confidence_threshold = confidence_threshold
        self._model = None

    def _load_model(self):
        if self._model is not None:
            return self._model
        try:
            import vosk  # type: ignore
        except Exception as exc:  # pragma: no cover - optional production dependency
            raise RuntimeError("vosk is not installed; install the local command recognizer dependency") from exc
        self._model = vosk.Model(self.model_path)
        return self._model

    async def recognize(self, audio_path: str | Path, registry: CommandRegistry, hinted_text: str | None = None) -> CommandMatch | None:
        if hinted_text:
            return registry.match_text(hinted_text)
        try:
            import vosk  # type: ignore
        except Exception as exc:  # pragma: no cover - optional production dependency
            raise RuntimeError("vosk is not installed; install the local command recognizer dependency") from exc
        model = self._load_model()
        path = Path(audio_path)
        with wave.open(str(path), "rb") as wav:
            recognizer = vosk.KaldiRecognizer(model, wav.getframerate())
            recognizer.SetWords(False)
            while True:
                chunk = wav.readframes(4000)
                if not chunk:
                    break
                recognizer.AcceptWaveform(chunk)
            result = json.loads(recognizer.FinalResult())
        transcript = str(result.get("text") or "").strip()
        if not transcript:
            return None
        match = registry.match_text(transcript)
        if not match:
            return None
        return CommandMatch(match.intent, match.alias, transcript, confidence=1.0)


def build_command_recognizer(config: CommandRegistryConfig) -> LocalCommandRecognizer:
    recognizer_config = config.recognizer
    if recognizer_config.engine == "vosk":
        if not recognizer_config.vosk_model_path:
            raise ValueError("vosk command recognizer requires command_registry.recognizer.vosk_model_path")
        return VoskCommandRecognizer(
            recognizer_config.vosk_model_path,
            confidence_threshold=recognizer_config.confidence_threshold,
        )
    return ConfiguredTextCommandRecognizer()


__all__ = [
    "CommandRegistry",
    "CommandMatch",
    "LocalCommandRecognizer",
    "ConfiguredTextCommandRecognizer",
    "VoskCommandRecognizer",
    "build_command_recognizer",
]
