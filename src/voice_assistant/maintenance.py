from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from .config import AssistantConfig
from .constants import EventType
from .telemetry import TelemetryStore


@dataclass(frozen=True)
class CommandResult:
    accepted: bool
    executed: bool
    returncode: int | None
    output: str


class MaintenanceController:
    def __init__(self, telemetry: TelemetryStore):
        self.telemetry = telemetry

    async def run_cleanup(self, cfg: AssistantConfig) -> dict[str, int]:
        result = self.telemetry.cleanup_older_than(cfg.telemetry.retention_days)
        self.telemetry.log_event(
            EventType.CLEANUP,
            "Telemetry/artifact cleanup completed.",
            component="maintenance",
            success=True,
            data=result,
        )
        return result

    async def restart_service(self, cfg: AssistantConfig, *, confirm: bool) -> CommandResult:
        if not confirm:
            self.telemetry.log_event(EventType.RESTART, "Assistant service restart rejected because confirmation was missing.", component="maintenance", success=False)
            return CommandResult(False, False, None, "confirmation required")
        return await self._execute(cfg, cfg.maintenance.assistant_restart_command, EventType.RESTART, "Assistant service restart requested.")

    async def reboot_machine(self, cfg: AssistantConfig, *, confirm: bool) -> CommandResult:
        if not confirm:
            self.telemetry.log_event(EventType.REBOOT, "Machine reboot rejected because confirmation was missing.", component="maintenance", success=False)
            return CommandResult(False, False, None, "confirmation required")
        return await self._execute(cfg, cfg.maintenance.machine_reboot_command, EventType.REBOOT, "Thin-client machine reboot requested.")

    async def _execute(self, cfg: AssistantConfig, command: list[str], event_type: EventType, message: str) -> CommandResult:
        if not cfg.maintenance.host_command_execution_enabled:
            self.telemetry.log_event(
                event_type,
                message + " Host command execution is disabled in configuration.",
                component="maintenance",
                success=False,
                data={"command": command, "executed": False},
            )
            return CommandResult(True, False, None, "host command execution disabled")
        if not command:
            self.telemetry.log_event(event_type, message + " No command is configured.", component="maintenance", success=False)
            return CommandResult(True, False, None, "no command configured")
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        output = out.decode(errors="replace")
        ok = proc.returncode == 0
        self.telemetry.log_event(
            event_type,
            message,
            component="maintenance",
            success=ok,
            error=None if ok else output,
            data={"command": command, "returncode": proc.returncode, "output": output[-2000:]},
        )
        return CommandResult(True, True, proc.returncode, output)
