"""Event helpers for task execution."""

from __future__ import annotations

from time import time


def build_event_payload(
    *,
    task_id: str,
    step: str,
    task_stage: int,
    seq_in_stage: int,
    progress: int,
    message: str,
    data: dict | None = None,
) -> dict:
    return {
        "task_id": task_id,
        "step": step,
        "task_stage": task_stage,
        "seq_in_stage": seq_in_stage,
        "progress": progress,
        "message": message,
        "ts_ms": int(time() * 1000),
        "data": data or {},
    }
