from __future__ import annotations

import threading
from dataclasses import dataclass

from .constants import EventType, RuntimeState
from .telemetry import TelemetryStore


@dataclass(frozen=True)
class RuntimeStatus:
    state: RuntimeState
    human_status: str


class StateManager:
    def __init__(self, telemetry: TelemetryStore):
        self._state = RuntimeState.IDLE
        self._lock = threading.RLock()
        self._telemetry = telemetry

    @property
    def state(self) -> RuntimeState:
        with self._lock:
            return self._state

    def set_state(self, state: RuntimeState, *, interaction_id: str | None = None, conversation_id: str | None = None) -> None:
        with self._lock:
            if state == self._state:
                return
            old = self._state
            self._state = state
        self._telemetry.log_event(
            EventType.STATE_CHANGED,
            f"Runtime state changed from {old.value} to {state.value}.",
            state=state.value,
            interaction_id=interaction_id,
            conversation_id=conversation_id,
            data={"old_state": old.value, "new_state": state.value},
        )

    def status(self) -> RuntimeStatus:
        with self._lock:
            state = self._state
        return RuntimeStatus(state=state, human_status=state.value)
