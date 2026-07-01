from __future__ import annotations

import abc
import asyncio
import contextlib
import json
import re
import shutil
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .config import CommandDefinition, CommandRegistryConfig


_WORD_RE = re.compile(r"[a-z0-9']+")
_DECODER_PREFIX_RE = re.compile(r"^\s*(?:\d+(?:\.\d+)?(?::|\s+))+\s*")


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


def _read_text_hint(audio_path: str | Path, hinted_text: str | None) -> str | None:
    if hinted_text is not None:
        return hinted_text
    sidecar = Path(str(audio_path) + ".command.txt")
    if sidecar.exists():
        return sidecar.read_text(encoding="utf-8")
    return None


class ConfiguredTextCommandRecognizer(LocalCommandRecognizer):
    """Local recognizer for tests, diagnostics, and integrations that provide a local transcript.

    It never calls the main STT service. When no local text hint or sidecar exists, it returns no
    command and the regular STT pipeline can process the prompt.
    """

    async def recognize(self, audio_path: str | Path, registry: CommandRegistry, hinted_text: str | None = None) -> CommandMatch | None:
        text = _read_text_hint(audio_path, hinted_text)
        if text is None:
            return None
        return registry.match_text(text)


class PocketsphinxCommandRecognizer(LocalCommandRecognizer):
    """Bounded local command recognizer using the packaged PocketSphinx decoder.

    This recognizer decodes only the post-wake prompt capture on the thin client, then performs the
    same whole-utterance alias match as every other local command recognizer. It never calls the
    downstream/main STT service and never uses the LLM to classify commands.
    """

    def __init__(
        self,
        *,
        command: list[str],
        hmm_path: str,
        dict_path: str,
        lm_path: str | None,
        timeout_seconds: float,
    ):
        self.command = list(command)
        self.hmm_path = hmm_path
        self.dict_path = dict_path
        self.lm_path = lm_path
        self.timeout_seconds = timeout_seconds

    async def recognize(self, audio_path: str | Path, registry: CommandRegistry, hinted_text: str | None = None) -> CommandMatch | None:
        text = _read_text_hint(audio_path, hinted_text)
        if text is not None:
            return registry.match_text(text)
        if not registry.enabled_commands():
            return None
        output = await self._decode_locally(Path(audio_path))
        return self._match_decoder_output(output, registry)

    async def _decode_locally(self, audio_path: Path) -> str:
        if not self.command:
            raise RuntimeError("pocketsphinx command recognizer command is empty")
        executable = self.command[0]
        if Path(executable).name == executable and shutil.which(executable) is None:
            raise RuntimeError(f"{executable} is not installed; local command audio cannot be decoded")
        with wave.open(str(audio_path), "rb") as wav:
            sample_rate = wav.getframerate()
            channels = wav.getnchannels()
            sample_width = wav.getsampwidth()
            if channels != 1 or sample_width != 2:
                raise RuntimeError("pocketsphinx command recognition requires 16-bit mono prompt audio")
            pcm = wav.readframes(wav.getnframes())
        command = [
            *self.command,
            "-infile",
            "/dev/stdin",
            "-samprate",
            str(sample_rate),
            "-hmm",
            self.hmm_path,
            "-dict",
            self.dict_path,
            "-logfn",
            "/dev/null",
        ]
        if self.lm_path:
            command.extend(["-lm", self.lm_path])
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(pcm), timeout=self.timeout_seconds)
        except asyncio.TimeoutError as exc:
            await self._terminate(proc)
            raise RuntimeError("pocketsphinx command recognition timed out") from exc
        output = b"\n".join(part for part in (stdout, stderr) if part).decode(errors="replace")
        if proc.returncode not in (0, None):
            raise RuntimeError(f"pocketsphinx command recognition failed with exit code {proc.returncode}: {output[-500:]}")
        return output

    async def _terminate(self, proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=1)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()

    def _match_decoder_output(self, output: str, registry: CommandRegistry) -> CommandMatch | None:
        for candidate in _candidate_transcripts_from_decoder_output(output):
            match = registry.match_text(candidate)
            if match:
                return match
        return None


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
        text = _read_text_hint(audio_path, hinted_text)
        if text is not None:
            return registry.match_text(text)
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


def _candidate_transcripts_from_decoder_output(output: str) -> Iterable[str]:
    """Yield possible hypotheses from PocketSphinx stdout/stderr in most-specific order."""

    lines = [line.strip() for line in output.splitlines() if line.strip()]
    for line in reversed(lines):
        if line.startswith("{"):
            with contextlib.suppress(json.JSONDecodeError):
                payload = json.loads(line)
                text = str(payload.get("text") or payload.get("hypothesis") or "").strip()
                if text:
                    yield text
        cleaned = line.replace("<s>", " ").replace("</s>", " ").replace("[SPEECH]", " ")
        cleaned = _DECODER_PREFIX_RE.sub("", cleaned).strip()
        if cleaned:
            yield cleaned


def build_command_recognizer(config: CommandRegistryConfig) -> LocalCommandRecognizer:
    recognizer_config = config.recognizer
    if recognizer_config.engine == "pocketsphinx":
        return PocketsphinxCommandRecognizer(
            command=recognizer_config.pocketsphinx_command,
            hmm_path=recognizer_config.pocketsphinx_hmm_path,
            dict_path=recognizer_config.pocketsphinx_dict_path,
            lm_path=recognizer_config.pocketsphinx_lm_path,
            timeout_seconds=recognizer_config.pocketsphinx_timeout_seconds,
        )
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
    "PocketsphinxCommandRecognizer",
    "VoskCommandRecognizer",
    "build_command_recognizer",
]
