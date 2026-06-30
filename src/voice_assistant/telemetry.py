from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from pydantic import BaseModel, Field

from .constants import ArtifactKind, EventType


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


def iso_now() -> str:
    return utc_now().isoformat()


class TelemetryEvent(BaseModel):
    id: str
    timestamp: str
    event_type: str
    state: str | None = None
    component: str | None = None
    stage: str | None = None
    conversation_id: str | None = None
    interaction_id: str | None = None
    command_intent: str | None = None
    success: bool | None = None
    error: str | None = None
    duration_ms: float | None = None
    human_message: str
    data: dict[str, Any] = Field(default_factory=dict)


class ArtifactRecord(BaseModel):
    id: str
    timestamp: str
    kind: str
    path: str
    filename: str
    content_type: str = "audio/wav"
    conversation_id: str | None = None
    interaction_id: str | None = None
    event_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True)
class TelemetryFilters:
    event_type: str | None = None
    start: str | None = None
    end: str | None = None
    errors_only: bool = False
    conversation_id: str | None = None
    interaction_id: str | None = None
    component: str | None = None
    command_intent: str | None = None
    stage: str | None = None
    search: str | None = None
    limit: int = 200
    offset: int = 0


class TelemetryStore:
    def __init__(self, db_path: str | Path, artifacts_dir: str | Path):
        self.db_path = Path(db_path)
        self.artifacts_dir = Path(artifacts_dir)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._subscribers: list[asyncio.Queue[TelemetryEvent]] = []
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY,
                    timestamp TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    state TEXT,
                    component TEXT,
                    stage TEXT,
                    conversation_id TEXT,
                    interaction_id TEXT,
                    command_intent TEXT,
                    success INTEGER,
                    error TEXT,
                    duration_ms REAL,
                    human_message TEXT NOT NULL,
                    data_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp);
                CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);
                CREATE INDEX IF NOT EXISTS idx_events_conversation ON events(conversation_id);
                CREATE INDEX IF NOT EXISTS idx_events_interaction ON events(interaction_id);
                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY,
                    timestamp TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    path TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    conversation_id TEXT,
                    interaction_id TEXT,
                    event_id TEXT,
                    metadata_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_artifacts_timestamp ON artifacts(timestamp);
                CREATE INDEX IF NOT EXISTS idx_artifacts_kind ON artifacts(kind);
                """
            )

    def log_event(
        self,
        event_type: EventType | str,
        human_message: str,
        *,
        state: str | None = None,
        component: str | None = None,
        stage: str | None = None,
        conversation_id: str | None = None,
        interaction_id: str | None = None,
        command_intent: str | None = None,
        success: bool | None = None,
        error: str | None = None,
        duration_ms: float | None = None,
        data: dict[str, Any] | None = None,
    ) -> TelemetryEvent:
        event = TelemetryEvent(
            id=str(uuid.uuid4()),
            timestamp=iso_now(),
            event_type=str(event_type),
            state=state,
            component=component,
            stage=stage,
            conversation_id=conversation_id,
            interaction_id=interaction_id,
            command_intent=command_intent,
            success=success,
            error=error,
            duration_ms=duration_ms,
            human_message=human_message,
            data=data or {},
        )
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO events (
                    id,timestamp,event_type,state,component,stage,conversation_id,interaction_id,
                    command_intent,success,error,duration_ms,human_message,data_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    event.id,
                    event.timestamp,
                    event.event_type,
                    event.state,
                    event.component,
                    event.stage,
                    event.conversation_id,
                    event.interaction_id,
                    event.command_intent,
                    None if event.success is None else int(event.success),
                    event.error,
                    event.duration_ms,
                    event.human_message,
                    json.dumps(event.data, sort_keys=True),
                ),
            )
        self._publish(event)
        return event

    def _publish(self, event: TelemetryEvent) -> None:
        stale: list[asyncio.Queue[TelemetryEvent]] = []
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                stale.append(queue)
        for queue in stale:
            if queue in self._subscribers:
                self._subscribers.remove(queue)

    def subscribe(self) -> asyncio.Queue[TelemetryEvent]:
        queue: asyncio.Queue[TelemetryEvent] = asyncio.Queue(maxsize=1000)
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[TelemetryEvent]) -> None:
        if queue in self._subscribers:
            self._subscribers.remove(queue)

    def query_events(self, filters: TelemetryFilters | None = None, **kwargs: Any) -> list[TelemetryEvent]:
        if filters is not None and kwargs:
            raise TypeError("pass either filters or keyword filters, not both")
        filters = filters or TelemetryFilters(**kwargs)
        clauses: list[str] = []
        params: list[Any] = []
        if filters.event_type:
            clauses.append("event_type = ?")
            params.append(filters.event_type)
        if filters.start:
            clauses.append("timestamp >= ?")
            params.append(filters.start)
        if filters.end:
            clauses.append("timestamp <= ?")
            params.append(filters.end)
        if filters.errors_only:
            clauses.append("error IS NOT NULL")
        if filters.conversation_id:
            clauses.append("conversation_id = ?")
            params.append(filters.conversation_id)
        if filters.interaction_id:
            clauses.append("interaction_id = ?")
            params.append(filters.interaction_id)
        if filters.component:
            clauses.append("component = ?")
            params.append(filters.component)
        if filters.command_intent:
            clauses.append("command_intent = ?")
            params.append(filters.command_intent)
        if filters.stage:
            clauses.append("stage = ?")
            params.append(filters.stage)
        if filters.search:
            clauses.append("(human_message LIKE ? OR data_json LIKE ? OR error LIKE ?)")
            like = f"%{filters.search}%"
            params.extend([like, like, like])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        limit = max(1, min(filters.limit, 1000))
        offset = max(0, filters.offset)
        query = f"SELECT * FROM events {where} ORDER BY timestamp DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self._lock, self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._event_from_row(row) for row in rows]

    def _event_from_row(self, row: sqlite3.Row) -> TelemetryEvent:
        return TelemetryEvent(
            id=row["id"],
            timestamp=row["timestamp"],
            event_type=row["event_type"],
            state=row["state"],
            component=row["component"],
            stage=row["stage"],
            conversation_id=row["conversation_id"],
            interaction_id=row["interaction_id"],
            command_intent=row["command_intent"],
            success=None if row["success"] is None else bool(row["success"]),
            error=row["error"],
            duration_ms=row["duration_ms"],
            human_message=row["human_message"],
            data=json.loads(row["data_json"]),
        )

    def create_artifact(
        self,
        source_path: str | Path,
        kind: ArtifactKind | str,
        *,
        conversation_id: str | None = None,
        interaction_id: str | None = None,
        event_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ArtifactRecord:
        source = Path(source_path)
        if not source.exists():
            raise FileNotFoundError(source)
        artifact_id = str(uuid.uuid4())
        suffix = source.suffix or ".wav"
        kind_str = str(kind)
        dest_dir = self.artifacts_dir / kind_str
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{artifact_id}{suffix}"
        shutil.copy2(source, dest)
        record = ArtifactRecord(
            id=artifact_id,
            timestamp=iso_now(),
            kind=kind_str,
            path=str(dest),
            filename=dest.name,
            conversation_id=conversation_id,
            interaction_id=interaction_id,
            event_id=event_id,
            metadata=metadata or {},
        )
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO artifacts (
                    id,timestamp,kind,path,filename,content_type,conversation_id,
                    interaction_id,event_id,metadata_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    record.id,
                    record.timestamp,
                    record.kind,
                    record.path,
                    record.filename,
                    record.content_type,
                    record.conversation_id,
                    record.interaction_id,
                    record.event_id,
                    json.dumps(record.metadata, sort_keys=True),
                ),
            )
        return record

    def get_artifact(self, artifact_id: str) -> ArtifactRecord | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
        if not row:
            return None
        return self._artifact_from_row(row)

    def list_artifacts(
        self,
        *,
        kind: str | None = None,
        conversation_id: str | None = None,
        interaction_id: str | None = None,
        limit: int = 200,
    ) -> list[ArtifactRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if conversation_id:
            clauses.append("conversation_id = ?")
            params.append(conversation_id)
        if interaction_id:
            clauses.append("interaction_id = ?")
            params.append(interaction_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(limit, 1000)))
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM artifacts {where} ORDER BY timestamp DESC LIMIT ?",
                params,
            ).fetchall()
        return [self._artifact_from_row(row) for row in rows]

    def _artifact_from_row(self, row: sqlite3.Row) -> ArtifactRecord:
        return ArtifactRecord(
            id=row["id"],
            timestamp=row["timestamp"],
            kind=row["kind"],
            path=row["path"],
            filename=row["filename"],
            content_type=row["content_type"],
            conversation_id=row["conversation_id"],
            interaction_id=row["interaction_id"],
            event_id=row["event_id"],
            metadata=json.loads(row["metadata_json"]),
        )

    def cleanup_older_than(self, retention_days: int) -> dict[str, int]:
        cutoff = (utc_now() - timedelta(days=retention_days)).isoformat()
        with self._lock, self._connect() as conn:
            old_artifacts = conn.execute(
                "SELECT id, path FROM artifacts WHERE timestamp < ?", (cutoff,)
            ).fetchall()
            removed_files = 0
            for row in old_artifacts:
                try:
                    Path(row["path"]).unlink(missing_ok=True)
                    removed_files += 1
                except OSError:
                    pass
            artifact_count = conn.execute(
                "DELETE FROM artifacts WHERE timestamp < ?", (cutoff,)
            ).rowcount
            event_count = conn.execute(
                "DELETE FROM events WHERE timestamp < ?", (cutoff,)
            ).rowcount
        return {"events_deleted": event_count, "artifacts_deleted": artifact_count, "files_deleted": removed_files}

    def insert_event_for_test(self, event: TelemetryEvent) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO events (
                    id,timestamp,event_type,state,component,stage,conversation_id,interaction_id,
                    command_intent,success,error,duration_ms,human_message,data_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    event.id,
                    event.timestamp,
                    event.event_type,
                    event.state,
                    event.component,
                    event.stage,
                    event.conversation_id,
                    event.interaction_id,
                    event.command_intent,
                    None if event.success is None else int(event.success),
                    event.error,
                    event.duration_ms,
                    event.human_message,
                    json.dumps(event.data, sort_keys=True),
                ),
            )


__all__ = [
    "TelemetryStore",
    "TelemetryEvent",
    "TelemetryFilters",
    "ArtifactRecord",
    "utc_now",
]
