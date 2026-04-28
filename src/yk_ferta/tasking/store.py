"""SQLite-backed persistence for tasks, events, and artifacts."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .models import CaseRecord, IdempotencyRecord, TaskArtifact, TaskEvent, TaskRecord


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SQLiteTaskStore:
    """Minimal SQLite store for yk-FERTA task orchestration."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_schema()

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.db_path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _init_schema(self) -> None:
        with self._lock, self._connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS cases (
                    case_id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    input_mode TEXT NOT NULL,
                    patient_payload TEXT NOT NULL,
                    manual_phenotypes TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL,
                    workflow_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    progress INTEGER NOT NULL,
                    search_depth INTEGER NOT NULL,
                    params TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    error_message TEXT,
                    failure_type TEXT,
                    metrics TEXT NOT NULL DEFAULT '{}',
                    idempotency_key TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(case_id) REFERENCES cases(case_id)
                );

                CREATE TABLE IF NOT EXISTS idempotency_keys (
                    endpoint TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_fingerprint TEXT NOT NULL,
                    resource_type TEXT NOT NULL,
                    resource_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(endpoint, idempotency_key)
                );

                CREATE TABLE IF NOT EXISTS task_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    step TEXT NOT NULL,
                    task_stage INTEGER NOT NULL,
                    seq_in_stage INTEGER NOT NULL,
                    progress INTEGER NOT NULL,
                    message TEXT NOT NULL,
                    ts_ms INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES tasks(task_id)
                );

                CREATE TABLE IF NOT EXISTS task_artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    artifact_type TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(task_id, artifact_type, version),
                    FOREIGN KEY(task_id) REFERENCES tasks(task_id)
                );
                """
            )
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
            if "failure_type" not in columns:
                conn.execute("ALTER TABLE tasks ADD COLUMN failure_type TEXT")
            if "metrics" not in columns:
                conn.execute("ALTER TABLE tasks ADD COLUMN metrics TEXT NOT NULL DEFAULT '{}'")
            if "idempotency_key" not in columns:
                conn.execute("ALTER TABLE tasks ADD COLUMN idempotency_key TEXT NOT NULL DEFAULT ''")
            case_columns = {row["name"] for row in conn.execute("PRAGMA table_info(cases)").fetchall()}
            if "idempotency_key" not in case_columns:
                conn.execute("ALTER TABLE cases ADD COLUMN idempotency_key TEXT NOT NULL DEFAULT ''")

    def create_case(
        self,
        *,
        case_id: str | None,
        source: str,
        input_mode: str,
        patient_payload: dict,
        manual_phenotypes: list[str] | None = None,
        idempotency_key: str = "",
    ) -> CaseRecord:
        record = CaseRecord(
            case_id=case_id or f"case_{uuid4().hex[:12]}",
            source=source,
            input_mode=input_mode,
            patient_payload=patient_payload,
            manual_phenotypes=manual_phenotypes or [],
            created_at=_utc_now_iso(),
            idempotency_key=idempotency_key,
        )
        with self._lock, self._connection() as conn:
            conn.execute(
                """
                INSERT INTO cases(case_id, source, input_mode, patient_payload, manual_phenotypes, created_at, idempotency_key)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.case_id,
                    record.source,
                    record.input_mode,
                    json.dumps(record.patient_payload, ensure_ascii=False),
                    json.dumps(record.manual_phenotypes, ensure_ascii=False),
                    record.created_at,
                    record.idempotency_key,
                ),
            )
        return record

    def get_case(self, case_id: str) -> CaseRecord | None:
        with self._lock, self._connection() as conn:
            row = conn.execute("SELECT * FROM cases WHERE case_id = ?", (case_id,)).fetchone()
        if row is None:
            return None
        return CaseRecord(
            case_id=row["case_id"],
            source=row["source"],
            input_mode=row["input_mode"],
            patient_payload=json.loads(row["patient_payload"]),
            manual_phenotypes=json.loads(row["manual_phenotypes"]),
            created_at=row["created_at"],
            idempotency_key=row["idempotency_key"] if "idempotency_key" in row.keys() else "",
        )

    def create_task(
        self,
        *,
        case_id: str,
        workflow_name: str = "clinical_mvp_v1",
        params: dict | None = None,
        idempotency_key: str = "",
    ) -> TaskRecord:
        record = TaskRecord(
            task_id=f"task_{uuid4().hex[:12]}",
            case_id=case_id,
            workflow_name=workflow_name,
            status="queued",
            stage="queued",
            progress=0,
            search_depth=1,
            params=params or {},
            started_at="",
            finished_at="",
            error_message=None,
            failure_type=None,
            metrics={},
            idempotency_key=idempotency_key,
        )
        with self._lock, self._connection() as conn:
            conn.execute(
                """
                INSERT INTO tasks(task_id, case_id, workflow_name, status, stage, progress, search_depth, params, started_at, finished_at, error_message, failure_type, metrics, idempotency_key)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.task_id,
                    record.case_id,
                    record.workflow_name,
                    record.status,
                    record.stage,
                    record.progress,
                    record.search_depth,
                    json.dumps(record.params, ensure_ascii=False),
                    record.started_at,
                    record.finished_at,
                    record.error_message,
                    record.failure_type,
                    json.dumps(record.metrics, ensure_ascii=False),
                    record.idempotency_key,
                ),
            )
        return record

    def get_task(self, task_id: str) -> TaskRecord | None:
        with self._lock, self._connection() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            return None
        return TaskRecord(
            task_id=row["task_id"],
            case_id=row["case_id"],
            workflow_name=row["workflow_name"],
            status=row["status"],
            stage=row["stage"],
            progress=row["progress"],
            search_depth=row["search_depth"],
            params=json.loads(row["params"]),
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            error_message=row["error_message"],
            failure_type=row["failure_type"],
            metrics=json.loads(row["metrics"] or "{}"),
            idempotency_key=row["idempotency_key"] if "idempotency_key" in row.keys() else "",
        )

    def list_tasks(self, *, case_id: str | None = None) -> list[TaskRecord]:
        query = "SELECT * FROM tasks"
        params: tuple = ()
        if case_id:
            query += " WHERE case_id = ?"
            params = (case_id,)
        query += " ORDER BY rowid DESC"
        with self._lock, self._connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            TaskRecord(
                task_id=row["task_id"],
                case_id=row["case_id"],
                workflow_name=row["workflow_name"],
                status=row["status"],
                stage=row["stage"],
                progress=row["progress"],
                search_depth=row["search_depth"],
                params=json.loads(row["params"]),
                started_at=row["started_at"],
                finished_at=row["finished_at"],
                error_message=row["error_message"],
                failure_type=row["failure_type"],
                metrics=json.loads(row["metrics"] or "{}"),
                idempotency_key=row["idempotency_key"] if "idempotency_key" in row.keys() else "",
            )
            for row in rows
        ]

    def update_task(
        self,
        task_id: str,
        *,
        status: str | None = None,
        stage: str | None = None,
        progress: int | None = None,
        search_depth: int | None = None,
        started_at: str | None = None,
        finished_at: str | None = None,
        error_message: str | None = None,
        failure_type: str | None = None,
        metrics: dict | None = None,
    ) -> None:
        current = self.get_task(task_id)
        if current is None:
            raise KeyError(f"Unknown task_id: {task_id}")
        updated = asdict(current)
        if status is not None:
            updated["status"] = status
        if stage is not None:
            updated["stage"] = stage
        if progress is not None:
            updated["progress"] = progress
        if search_depth is not None:
            updated["search_depth"] = search_depth
        if started_at is not None:
            updated["started_at"] = started_at
        if finished_at is not None:
            updated["finished_at"] = finished_at
        if error_message is not None or status == "failed":
            updated["error_message"] = error_message
        if failure_type is not None or status != "failed":
            updated["failure_type"] = failure_type
        if metrics is not None:
            updated["metrics"] = metrics
        with self._lock, self._connection() as conn:
            conn.execute(
                """
                UPDATE tasks
                SET status = ?, stage = ?, progress = ?, search_depth = ?, params = ?, started_at = ?, finished_at = ?, error_message = ?, failure_type = ?, metrics = ?
                WHERE task_id = ?
                """,
                (
                    updated["status"],
                    updated["stage"],
                    updated["progress"],
                    updated["search_depth"],
                    json.dumps(updated["params"], ensure_ascii=False),
                    updated["started_at"],
                    updated["finished_at"],
                    updated["error_message"],
                    updated["failure_type"],
                    json.dumps(updated["metrics"], ensure_ascii=False),
                    task_id,
                ),
            )

    def append_event(self, payload: dict) -> TaskEvent:
        with self._lock, self._connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO task_events(task_id, step, task_stage, seq_in_stage, progress, message, ts_ms, data)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["task_id"],
                    payload["step"],
                    payload["task_stage"],
                    payload["seq_in_stage"],
                    payload["progress"],
                    payload["message"],
                    payload["ts_ms"],
                    json.dumps(payload.get("data", {}), ensure_ascii=False),
                ),
            )
            event_id = int(cursor.lastrowid)
        return TaskEvent(
            event_id=event_id,
            task_id=payload["task_id"],
            step=payload["step"],
            task_stage=payload["task_stage"],
            seq_in_stage=payload["seq_in_stage"],
            progress=payload["progress"],
            message=payload["message"],
            ts_ms=payload["ts_ms"],
            data=payload.get("data", {}),
        )

    def get_idempotency_record(self, endpoint: str, idempotency_key: str) -> IdempotencyRecord | None:
        with self._lock, self._connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM idempotency_keys
                WHERE endpoint = ? AND idempotency_key = ?
                """,
                (endpoint, idempotency_key),
            ).fetchone()
        if row is None:
            return None
        return IdempotencyRecord(
            endpoint=row["endpoint"],
            idempotency_key=row["idempotency_key"],
            request_fingerprint=row["request_fingerprint"],
            resource_type=row["resource_type"],
            resource_id=row["resource_id"],
            created_at=row["created_at"],
        )

    def save_idempotency_record(
        self,
        *,
        endpoint: str,
        idempotency_key: str,
        request_fingerprint: str,
        resource_type: str,
        resource_id: str,
    ) -> IdempotencyRecord:
        record = IdempotencyRecord(
            endpoint=endpoint,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            resource_type=resource_type,
            resource_id=resource_id,
            created_at=_utc_now_iso(),
        )
        with self._lock, self._connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO idempotency_keys(
                    endpoint, idempotency_key, request_fingerprint, resource_type, resource_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    record.endpoint,
                    record.idempotency_key,
                    record.request_fingerprint,
                    record.resource_type,
                    record.resource_id,
                    record.created_at,
                ),
            )
        return record

    def list_events(self, task_id: str, *, after_event_id: int = 0) -> list[TaskEvent]:
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM task_events
                WHERE task_id = ? AND event_id > ?
                ORDER BY event_id ASC
                """,
                (task_id, after_event_id),
            ).fetchall()
        return [
            TaskEvent(
                event_id=row["event_id"],
                task_id=row["task_id"],
                step=row["step"],
                task_stage=row["task_stage"],
                seq_in_stage=row["seq_in_stage"],
                progress=row["progress"],
                message=row["message"],
                ts_ms=row["ts_ms"],
                data=json.loads(row["data"]),
            )
            for row in rows
        ]

    def save_artifact(
        self,
        *,
        task_id: str,
        artifact_type: str,
        data: dict,
        version: int = 1,
    ) -> TaskArtifact:
        record = TaskArtifact(
            artifact_id=f"art_{uuid4().hex[:12]}",
            task_id=task_id,
            artifact_type=artifact_type,
            version=version,
            data=data,
            created_at=_utc_now_iso(),
        )
        with self._lock, self._connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO task_artifacts(artifact_id, task_id, artifact_type, version, data, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    record.artifact_id,
                    record.task_id,
                    record.artifact_type,
                    record.version,
                    json.dumps(record.data, ensure_ascii=False),
                    record.created_at,
                ),
            )
        return record

    def list_artifacts(self, task_id: str) -> list[TaskArtifact]:
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM task_artifacts WHERE task_id = ?
                ORDER BY artifact_type ASC, version DESC
                """,
                (task_id,),
            ).fetchall()
        return [
            TaskArtifact(
                artifact_id=row["artifact_id"],
                task_id=row["task_id"],
                artifact_type=row["artifact_type"],
                version=row["version"],
                data=json.loads(row["data"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def get_artifact(self, task_id: str, artifact_type: str) -> TaskArtifact | None:
        with self._lock, self._connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM task_artifacts
                WHERE task_id = ? AND artifact_type = ?
                ORDER BY version DESC
                LIMIT 1
                """,
                (task_id, artifact_type),
            ).fetchone()
        if row is None:
            return None
        return TaskArtifact(
            artifact_id=row["artifact_id"],
            task_id=row["task_id"],
            artifact_type=row["artifact_type"],
            version=row["version"],
            data=json.loads(row["data"]),
            created_at=row["created_at"],
        )
