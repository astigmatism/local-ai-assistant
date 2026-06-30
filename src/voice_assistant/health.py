from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx

from .config import AssistantConfig


@dataclass(frozen=True)
class HealthItem:
    component: str
    ok: bool
    detail: str


async def _run_command(command: list[str], timeout: float = 5.0) -> tuple[int, str]:
    if shutil.which(command[0]) is None:
        return 127, f"{command[0]} not installed"
    proc = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "command timed out"
    return proc.returncode or 0, out.decode(errors="replace")


def _models_url_from_transcription_url(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path
    if "/v1/" in path:
        base = path.split("/v1/", 1)[0]
        path = base + "/v1/models"
    else:
        path = "/v1/models"
    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


class HealthChecker:
    def __init__(self, cfg: AssistantConfig):
        self.cfg = cfg

    async def check_all(self) -> dict[str, Any]:
        checks = [
            await self.check_wake_engine(),
            await self.check_command_recognizer(),
            await self.check_capture_device(),
            await self.check_playback_device(),
            await self.check_mixer(),
            await self.check_stt(),
            await self.check_llm(),
            await self.check_tts(),
        ]
        return {"ok": all(item.ok for item in checks), "checks": [item.__dict__ for item in checks]}

    async def check_wake_engine(self) -> HealthItem:
        engine = self.cfg.wake.engine
        if engine == "openwakeword":
            try:
                import openwakeword  # type: ignore  # noqa: F401
                return HealthItem("wake-word engine", True, "openWakeWord import available")
            except Exception as exc:
                return HealthItem("wake-word engine", False, f"openWakeWord unavailable: {exc}")
        if engine == "external_command":
            ok = bool(self.cfg.wake.external_command and shutil.which(self.cfg.wake.external_command[0]))
            return HealthItem("wake-word engine", ok, "external command configured" if ok else "external command missing")
        return HealthItem("wake-word engine", True, "simulated wake engine active for tests/admin diagnostics")

    async def check_command_recognizer(self) -> HealthItem:
        recognizer = self.cfg.command_registry.recognizer
        if recognizer.engine == "vosk":
            if not recognizer.vosk_model_path:
                return HealthItem("local command recognizer", False, "Vosk model path is not configured")
            try:
                import vosk  # type: ignore  # noqa: F401
                return HealthItem("local command recognizer", True, "Vosk import available")
            except Exception as exc:
                return HealthItem("local command recognizer", False, f"Vosk unavailable: {exc}")
        return HealthItem("local command recognizer", True, "configured text recognizer available for tests/diagnostics")

    async def check_capture_device(self) -> HealthItem:
        code, output = await _run_command(["arecord", "-l"])
        ok = code == 0 and bool(output.strip())
        return HealthItem("ALSA capture device", ok, "capture devices visible" if ok else output.strip())

    async def check_playback_device(self) -> HealthItem:
        code, output = await _run_command(["aplay", "-l"])
        ok = code == 0 and bool(output.strip())
        return HealthItem("ALSA playback device", ok, "playback devices visible" if ok else output.strip())

    async def check_mixer(self) -> HealthItem:
        code, output = await _run_command(["amixer", "-c", str(self.cfg.audio.mixer_card_index)])
        if code != 0:
            return HealthItem("mixer volume", False, output.strip())
        lowered = output.lower()
        if "pcm" in lowered and "[0%]" in lowered:
            return HealthItem("mixer volume", False, "PCM mixer appears to be 0%")
        return HealthItem("mixer volume", True, "mixer readable and not obviously muted")

    async def check_stt(self) -> HealthItem:
        url = _models_url_from_transcription_url(self.cfg.services.stt.url)
        headers = {}
        key = os.getenv(self.cfg.services.stt.api_key_env)
        if key:
            headers["Authorization"] = f"Bearer {key}"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(url, headers=headers)
            if response.status_code in (401, 403):
                return HealthItem("STT service", False, f"authentication failed with HTTP {response.status_code}")
            return HealthItem("STT service", response.status_code < 500, f"HTTP {response.status_code}")
        except Exception as exc:
            return HealthItem("STT service", False, str(exc))

    async def check_llm(self) -> HealthItem:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(self.cfg.services.llm.health_url)
            return HealthItem("LLM service", response.status_code < 500, f"HTTP {response.status_code}")
        except Exception as exc:
            return HealthItem("LLM service", False, str(exc))

    async def check_tts(self) -> HealthItem:
        # The speech endpoint may return 405/422 to GET; that still proves routing. Auth is only
        # asserted when the service returns a clear 401/403.
        headers = {}
        key = os.getenv(self.cfg.services.tts.api_key_env)
        if key:
            headers["Authorization"] = f"Bearer {key}"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(self.cfg.services.tts.url, headers=headers)
            if response.status_code in (401, 403):
                return HealthItem("TTS service", False, f"authentication failed with HTTP {response.status_code}")
            return HealthItem("TTS service", response.status_code < 500, f"HTTP {response.status_code}")
        except Exception as exc:
            return HealthItem("TTS service", False, str(exc))
