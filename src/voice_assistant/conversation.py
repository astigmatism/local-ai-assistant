from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from .telemetry import utc_now

Role = Literal["system", "user", "assistant"]


@dataclass
class Message:
    role: Role
    content: str


@dataclass
class ConversationSnapshot:
    conversation_id: str
    messages: list[dict[str, str]]
    last_response_finished_at: str | None


class ConversationManager:
    """Maintains thin-client conversation context until explicit reset or inactivity timeout.

    It intentionally does not summarize or truncate history; backend LLM/model layers remain
    responsible for context-window management.
    """

    def __init__(self, system_prompt: str, inactivity_timeout_seconds: float):
        self.system_prompt = system_prompt
        self.inactivity_timeout_seconds = inactivity_timeout_seconds
        self._lock = threading.RLock()
        self._conversation_id = str(uuid.uuid4())
        self._messages: list[Message] = [Message("system", system_prompt)]
        self._last_response_finished_at: datetime | None = None

    @property
    def conversation_id(self) -> str:
        with self._lock:
            return self._conversation_id

    def add_user(self, text: str) -> None:
        with self._lock:
            self._messages.append(Message("user", text))
            self._last_response_finished_at = None

    def add_assistant(self, text: str) -> None:
        with self._lock:
            self._messages.append(Message("assistant", text))

    def mark_response_finished(self, when: datetime | None = None) -> None:
        with self._lock:
            self._last_response_finished_at = when or utc_now()

    def reset(self) -> str:
        with self._lock:
            self._conversation_id = str(uuid.uuid4())
            self._messages = [Message("system", self.system_prompt)]
            self._last_response_finished_at = None
            return self._conversation_id

    def expire_if_needed(self, now: datetime | None = None) -> bool:
        with self._lock:
            if self._last_response_finished_at is None:
                return False
            now = now or utc_now()
            elapsed = (now - self._last_response_finished_at).total_seconds()
            if elapsed <= self.inactivity_timeout_seconds:
                return False
            self._conversation_id = str(uuid.uuid4())
            self._messages = [Message("system", self.system_prompt)]
            self._last_response_finished_at = None
            return True

    def messages_for_llm(self) -> list[dict[str, str]]:
        with self._lock:
            return [{"role": message.role, "content": message.content} for message in self._messages]

    def snapshot(self) -> ConversationSnapshot:
        with self._lock:
            return ConversationSnapshot(
                conversation_id=self._conversation_id,
                messages=[{"role": message.role, "content": message.content} for message in self._messages],
                last_response_finished_at=(
                    self._last_response_finished_at.isoformat() if self._last_response_finished_at else None
                ),
            )
