from __future__ import annotations

import abc
import asyncio
import contextlib
import json
import math
import re
import shutil
import tempfile
import wave
from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .config import CommandDefinition, CommandRegistryConfig


_WORD_RE = re.compile(r"[a-z0-9']+")
_DECODER_PREFIX_RE = re.compile(r"^\s*(?:\d+(?:\.\d+)?(?::|\s+))+\s*")


@dataclass(frozen=True)
class SpeechActivity:
    active_seconds: float
    active_span_seconds: float
    rms_peak: float


@dataclass(frozen=True)
class PromptAudio:
    pcm: bytes
    sample_rate: int
    channels: int
    sample_width: int
    speech_activity: SpeechActivity


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
        keyphrase_threshold: str = "1e-20",
        speech_rms_threshold: int = 500,
        keyphrase_seconds_per_word: float = 0.65,
        keyphrase_padding_seconds: float = 0.45,
        keyphrase_max_speech_seconds: float = 2.75,
    ):
        self.command = list(command)
        self.hmm_path = hmm_path
        self.dict_path = dict_path
        self.lm_path = lm_path
        self.timeout_seconds = timeout_seconds
        self.keyphrase_threshold = keyphrase_threshold
        self.speech_rms_threshold = speech_rms_threshold
        self.keyphrase_seconds_per_word = keyphrase_seconds_per_word
        self.keyphrase_padding_seconds = keyphrase_padding_seconds
        self.keyphrase_max_speech_seconds = keyphrase_max_speech_seconds
        self.last_diagnostics: dict[str, object] = {}

    async def recognize(self, audio_path: str | Path, registry: CommandRegistry, hinted_text: str | None = None) -> CommandMatch | None:
        self.last_diagnostics = {"engine": "pocketsphinx"}
        text = _read_text_hint(audio_path, hinted_text)
        if text is not None:
            match = registry.match_text(text)
            self.last_diagnostics.update({"source": "text_hint", "matched": bool(match)})
            return match
        if not registry.enabled_commands():
            self.last_diagnostics.update({"source": "audio", "matched": False, "reason": "no_enabled_commands"})
            return None

        prompt_audio = self._read_prompt_audio(Path(audio_path))
        language_output = await self._decode_with_language_model(prompt_audio)
        language_candidates = list(_candidate_transcripts_from_decoder_output(language_output))
        self.last_diagnostics.update(
            {
                "source": "audio",
                "language_candidate_count": len(language_candidates),
                "speech_active_span_seconds": round(prompt_audio.speech_activity.active_span_seconds, 3),
                "speech_active_seconds": round(prompt_audio.speech_activity.active_seconds, 3),
                "rms_peak": round(prompt_audio.speech_activity.rms_peak, 1),
            }
        )
        language_match = self._match_candidates(language_candidates, registry)
        if language_match:
            self.last_diagnostics.update({"matched": True, "path": "language_model", "alias": language_match.alias})
            return language_match

        keyphrase_match = await self._recognize_with_keyphrase_search(prompt_audio, registry, language_candidates)
        if keyphrase_match:
            self.last_diagnostics.update({"matched": True, "path": "keyphrase", "alias": keyphrase_match.alias})
            return keyphrase_match
        self.last_diagnostics.setdefault("matched", False)
        return None

    def _read_prompt_audio(self, audio_path: Path) -> PromptAudio:
        with wave.open(str(audio_path), "rb") as wav:
            sample_rate = wav.getframerate()
            channels = wav.getnchannels()
            sample_width = wav.getsampwidth()
            if channels != 1 or sample_width != 2:
                raise RuntimeError("pocketsphinx command recognition requires 16-bit mono prompt audio")
            pcm = wav.readframes(wav.getnframes())
        return PromptAudio(
            pcm=pcm,
            sample_rate=sample_rate,
            channels=channels,
            sample_width=sample_width,
            speech_activity=_measure_speech_activity(
                pcm,
                sample_rate=sample_rate,
                rms_threshold=self.speech_rms_threshold,
            ),
        )

    async def _decode_with_language_model(self, prompt_audio: PromptAudio) -> str:
        if not self.lm_path:
            return ""
        return await self._run_decoder(
            prompt_audio,
            ["-lm", self.lm_path],
            failure_label="language model command recognition",
        )

    async def _recognize_with_keyphrase_search(
        self,
        prompt_audio: PromptAudio,
        registry: CommandRegistry,
        language_candidates: list[str],
    ) -> CommandMatch | None:
        alias_rows = _enabled_alias_rows(registry)
        if not alias_rows:
            return None
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".kws", delete=True) as kws_file:
            for row in alias_rows:
                kws_file.write(str(row["normalized"]) + "\n")
            kws_file.flush()
            output = await self._run_decoder(
                prompt_audio,
                ["-kws", kws_file.name, "-kws_threshold", self.keyphrase_threshold],
                failure_label="keyphrase command recognition",
            )
        candidates = list(_candidate_transcripts_from_decoder_output(output))
        self.last_diagnostics["keyphrase_candidate_count"] = len(candidates)
        for candidate in candidates:
            candidate_words = _normalised_words(candidate)
            for row in alias_rows:
                alias_words = row["words"]
                if not isinstance(alias_words, list):
                    continue
                if not _contains_word_sequence(candidate_words, alias_words):
                    continue
                alias_word_count = len(alias_words)
                if len(candidate_words) > alias_word_count:
                    self.last_diagnostics.update(
                        {
                            "matched": False,
                            "keyphrase_detected_alias": row["alias"],
                            "reason": "keyphrase_candidate_heard_more_than_command_alias",
                        }
                    )
                    return None
                if _language_candidates_are_longer_than_alias(language_candidates, alias_word_count):
                    self.last_diagnostics.update(
                        {
                            "matched": False,
                            "keyphrase_detected_alias": row["alias"],
                            "reason": "language_model_heard_more_than_command_alias",
                        }
                    )
                    return None
                speech_limit = self._speech_limit_for_alias(alias_word_count)
                if prompt_audio.speech_activity.active_span_seconds > speech_limit:
                    self.last_diagnostics.update(
                        {
                            "matched": False,
                            "keyphrase_detected_alias": row["alias"],
                            "reason": "speech_span_too_long_for_command_alias",
                            "speech_limit_seconds": round(speech_limit, 3),
                        }
                    )
                    return None
                return CommandMatch(
                    intent=str(row["intent"]),
                    alias=str(row["alias"]),
                    transcript=candidate,
                    confidence=1.0,
                )
        return None

    def _speech_limit_for_alias(self, alias_word_count: int) -> float:
        return min(
            self.keyphrase_max_speech_seconds,
            self.keyphrase_padding_seconds + alias_word_count * self.keyphrase_seconds_per_word,
        )

    async def _run_decoder(self, prompt_audio: PromptAudio, search_args: list[str], *, failure_label: str) -> str:
        if not self.command:
            raise RuntimeError("pocketsphinx command recognizer command is empty")
        executable = self.command[0]
        if Path(executable).name == executable and shutil.which(executable) is None:
            raise RuntimeError(f"{executable} is not installed; local command audio cannot be decoded")
        command = [
            *self.command,
            "-infile",
            "/dev/stdin",
            "-samprate",
            str(prompt_audio.sample_rate),
            "-hmm",
            self.hmm_path,
            "-dict",
            self.dict_path,
            "-logfn",
            "/dev/null",
            *search_args,
        ]
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(prompt_audio.pcm), timeout=self.timeout_seconds)
        except asyncio.TimeoutError as exc:
            await self._terminate(proc)
            raise RuntimeError(f"pocketsphinx {failure_label} timed out") from exc
        output = b"\n".join(part for part in (stdout, stderr) if part).decode(errors="replace")
        if proc.returncode not in (0, None):
            raise RuntimeError(f"pocketsphinx {failure_label} failed with exit code {proc.returncode}: {output[-500:]}")
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

    def _match_candidates(self, candidates: Iterable[str], registry: CommandRegistry) -> CommandMatch | None:
        for candidate in candidates:
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




def _normalised_words(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower().strip())


def _contains_word_sequence(words: list[str], sequence: list[str]) -> bool:
    if not words or not sequence or len(sequence) > len(words):
        return False
    width = len(sequence)
    return any(words[index : index + width] == sequence for index in range(0, len(words) - width + 1))


def _enabled_alias_rows(registry: CommandRegistry) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    for command in registry.enabled_commands():
        for alias in command.aliases:
            normalized = CommandRegistry.normalize(alias)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            rows.append(
                {
                    "intent": command.intent.value,
                    "alias": alias,
                    "normalized": normalized,
                    "words": normalized.split(),
                }
            )
    return rows


def _language_candidates_are_longer_than_alias(candidates: list[str], alias_word_count: int) -> bool:
    for candidate in candidates:
        words = _normalised_words(candidate)
        if len(words) > alias_word_count:
            return True
    return False


def _measure_speech_activity(pcm: bytes, *, sample_rate: int, rms_threshold: int, chunk_seconds: float = 0.05) -> SpeechActivity:
    if not pcm:
        return SpeechActivity(active_seconds=0.0, active_span_seconds=0.0, rms_peak=0.0)
    bytes_per_sample = 2
    chunk_bytes = max(bytes_per_sample, int(sample_rate * chunk_seconds) * bytes_per_sample)
    active_indexes: list[int] = []
    rms_peak = 0.0
    for index, start in enumerate(range(0, len(pcm), chunk_bytes)):
        chunk = pcm[start : start + chunk_bytes]
        rms = _rms_int16_le(chunk)
        rms_peak = max(rms_peak, rms)
        if rms > rms_threshold:
            active_indexes.append(index)
    if not active_indexes:
        return SpeechActivity(active_seconds=0.0, active_span_seconds=0.0, rms_peak=rms_peak)
    active_seconds = len(active_indexes) * chunk_seconds
    active_span_seconds = (active_indexes[-1] - active_indexes[0] + 1) * chunk_seconds
    return SpeechActivity(active_seconds=active_seconds, active_span_seconds=active_span_seconds, rms_peak=rms_peak)


def _rms_int16_le(data: bytes) -> float:
    if not data:
        return 0.0
    usable = data[: len(data) - (len(data) % 2)]
    if not usable:
        return 0.0
    samples = array("h")
    samples.frombytes(usable)
    if not samples:
        return 0.0
    return math.sqrt(sum(sample * sample for sample in samples) / len(samples))


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
            keyphrase_threshold=recognizer_config.pocketsphinx_keyphrase_threshold,
            speech_rms_threshold=recognizer_config.pocketsphinx_speech_rms_threshold,
            keyphrase_seconds_per_word=recognizer_config.pocketsphinx_keyphrase_seconds_per_word,
            keyphrase_padding_seconds=recognizer_config.pocketsphinx_keyphrase_padding_seconds,
            keyphrase_max_speech_seconds=recognizer_config.pocketsphinx_keyphrase_max_speech_seconds,
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
