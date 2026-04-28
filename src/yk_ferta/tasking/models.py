"""Dataclasses for persisted case/task/event/artifact records."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class CaseRecord:
    case_id: str
    source: str
    input_mode: str
    patient_payload: dict
    manual_phenotypes: list[dict | str] = field(default_factory=list)
    created_at: str = ""
    idempotency_key: str = ""


@dataclass(slots=True)
class TaskRecord:
    task_id: str
    case_id: str
    workflow_name: str
    status: str
    stage: str
    progress: int
    search_depth: int = 1
    params: dict = field(default_factory=dict)
    started_at: str = ""
    finished_at: str = ""
    error_message: str | None = None
    failure_type: str | None = None
    metrics: dict = field(default_factory=dict)
    idempotency_key: str = ""


@dataclass(slots=True)
class IdempotencyRecord:
    endpoint: str
    idempotency_key: str
    request_fingerprint: str
    resource_type: str
    resource_id: str
    created_at: str = ""


@dataclass(slots=True)
class TaskEvent:
    event_id: int
    task_id: str
    step: str
    task_stage: int
    seq_in_stage: int
    progress: int
    message: str
    ts_ms: int
    data: dict = field(default_factory=dict)


@dataclass(slots=True)
class TaskArtifact:
    artifact_id: str
    task_id: str
    artifact_type: str
    version: int
    data: dict
    created_at: str = ""
