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
    severity: str = "ok"


async def _run_command(command: list[str], timeout: float = 5.0, env: dict[str, str] | None = None) -> tuple[int, str]:
    if not command:
        return 127, "command missing"
    if shutil.which(command[0]) is None:
        return 127, f"{command[0]} not installed"
    proc = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
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
    def __init__(self, cfg: AssistantConfig, wake_runtime_status: dict[str, Any] | None = None):
        self.cfg = cfg
        self.wake_runtime_status = wake_runtime_status

    async def check_all(self) -> dict[str, Any]:
        checks = [
            await self.check_wake_engine(),
            await self.check_wake_runtime(),
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
            except Exception as exc:
                return HealthItem("wake-word engine", False, f"openWakeWord unavailable: {exc}", "error")
            if self.cfg.wake.model_path and not os.path.exists(self.cfg.wake.model_path):
                return HealthItem("wake-word engine", False, f"openWakeWord model path missing: {self.cfg.wake.model_path}", "error")
            return HealthItem("wake-word engine", True, "openWakeWord import and configured model path available")
        if engine == "external_command":
            if not self.cfg.wake.external_command:
                return HealthItem("wake-word engine", False, "external command missing", "error")
            executable = self.cfg.wake.external_command[0]
            if shutil.which(executable) is None:
                return HealthItem("wake-word engine", False, f"external command executable not found: {executable}", "error")
            if self.cfg.wake.external_health_command:
                env = os.environ.copy()
                env.update(
                    {
                        "VOICE_ASSISTANT_WAKE_PHRASE": self.cfg.wake.active_wake_phrase,
                        "VOICE_ASSISTANT_WAKE_SENSITIVITY": str(self.cfg.wake.sensitivity),
                        "VOICE_ASSISTANT_CAPTURE_DEVICE": self.cfg.audio.capture_device,
                        "VOICE_ASSISTANT_SAMPLE_RATE_HZ": str(self.cfg.audio.sample_rate_hz),
                        "VOICE_ASSISTANT_CHANNELS": str(self.cfg.audio.channels),
                    }
                )
                if self.cfg.wake.model_path:
                    env["VOICE_ASSISTANT_WAKE_MODEL_PATH"] = self.cfg.wake.model_path
                code, output = await _run_command(self.cfg.wake.external_health_command, timeout=8.0, env=env)
                if code != 0:
                    return HealthItem("wake-word engine", False, output.strip() or f"health command exited with {code}", "error")
                return HealthItem("wake-word engine", True, output.strip() or "external wake health command passed")
            return HealthItem("wake-word engine", True, "external wake command executable is configured")
        return HealthItem(
            "wake-word engine",
            False,
            "simulated wake engine is active; this is admin/test diagnostics only and is not hands-free production wake",
            "warning",
        )

    async def check_wake_runtime(self) -> HealthItem:
        status = self.wake_runtime_status
        if status is None:
            return HealthItem("wake-word runtime", True, "runtime status not supplied to health checker", "warning")
        if self.cfg.wake.engine == "simulated":
            return HealthItem("wake-word runtime", False, "simulated/admin trigger queue is available but no real listener is running", "warning")
        if not status.get("task_running"):
            detail = str(status.get("task_error") or "wake listener task is not running")
            return HealthItem("wake-word runtime", False, detail, "error")
        if status.get("paused"):
            return HealthItem("wake-word runtime", True, f"wake listener is temporarily paused: {status.get('paused_reason')}", "warning")
        if status.get("engine") == "external_command" and not status.get("process_running"):
            return HealthItem("wake-word runtime", False, str(status.get("last_error") or "external wake process is not running"), "error")
        return HealthItem("wake-word runtime", True, "wake listener task is running with a production engine")

    async def check_command_recognizer(self) -> HealthItem:
        enabled = [command for command in self.cfg.command_registry.commands if command.enabled]
        alias_count = sum(len(command.aliases) for command in enabled)
        if not enabled or alias_count == 0:
            return HealthItem(
                "local command recognizer",
                False,
                "STT-first command routing is active, but no enabled local command aliases are configured",
                "warning",
            )
        return HealthItem(
            "local command recognizer",
            True,
            (
                "STT-first command routing is active: configured STT transcribes prompts before "
                f"whole-utterance matching across {len(enabled)} enabled command(s) and {alias_count} alias(es). "
                "command_registry.recognizer is retained for diagnostics/backward compatibility and is not required for cancel/stop routing."
            ),
        )

    async def check_capture_device(self) -> HealthItem:
        code, output = await _run_command(["arecord", "-l"])
        ok = code == 0 and bool(output.strip())
        return HealthItem("ALSA capture device", ok, "capture devices visible" if ok else output.strip(), "ok" if ok else "error")

    async def check_playback_device(self) -> HealthItem:
        code, output = await _run_command(["aplay", "-l"])
        ok = code == 0 and bool(output.strip())
        return HealthItem("ALSA playback device", ok, "playback devices visible" if ok else output.strip(), "ok" if ok else "error")

    async def check_mixer(self) -> HealthItem:
        code, output = await _run_command(["amixer", "-c", str(self.cfg.audio.mixer_card_index)])
        if code != 0:
            return HealthItem("mixer volume", False, output.strip(), "error")
        lowered = output.lower()
        if "pcm" in lowered and "[0%]" in lowered:
            return HealthItem("mixer volume", False, "PCM mixer appears to be 0%", "error")
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
                return HealthItem("STT service", False, f"authentication failed with HTTP {response.status_code}", "error")
            return HealthItem("STT service", response.status_code < 500, f"HTTP {response.status_code}", "ok" if response.status_code < 500 else "error")
        except Exception as exc:
            return HealthItem("STT service", False, str(exc), "error")

    async def check_llm(self) -> HealthItem:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(self.cfg.services.llm.health_url)
            return HealthItem("LLM service", response.status_code < 500, f"HTTP {response.status_code}", "ok" if response.status_code < 500 else "error")
        except Exception as exc:
            return HealthItem("LLM service", False, str(exc), "error")

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
                return HealthItem("TTS service", False, f"authentication failed with HTTP {response.status_code}", "error")
            return HealthItem("TTS service", response.status_code < 500, f"HTTP {response.status_code}", "ok" if response.status_code < 500 else "error")
        except Exception as exc:
            return HealthItem("TTS service", False, str(exc), "error")
