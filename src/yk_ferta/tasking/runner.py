"""Background runner for task-managed clinical MVP execution."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

from yk_ferta.agents.factory import build_clinical_mvp_pipeline
from yk_ferta.config import ClinicalMvpConfig
from yk_ferta.schemas.clinical import PatientProfile, PhenotypeItem
from yk_ferta.tasking.events import build_event_payload
from yk_ferta.tasking.stages import TaskExecutionContext, WorkflowStage, build_default_stages
from yk_ferta.tasking.store import SQLiteTaskStore


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ClinicalMvpTaskRunner:
    """Execute the clinical MVP workflow with task/event persistence."""

    def __init__(
        self,
        store: SQLiteTaskStore,
        *,
        default_config_path: str = "config/clinical_mvp.json",
        stages: list[WorkflowStage] | None = None,
    ) -> None:
        self.store = store
        self.default_config_path = default_config_path
        self.stages = stages or build_default_stages()
        self._threads: dict[str, threading.Thread] = {}

    def start(self, task_id: str) -> None:
        thread = threading.Thread(target=self.run, args=(task_id,), daemon=True, name=f"yk-ferta-{task_id}")
        self._threads[task_id] = thread
        thread.start()

    def run(self, task_id: str) -> None:
        context = self._build_context(task_id)
        self.store.update_task(
            task_id,
            status="running",
            stage="case_ingestion",
            progress=1,
            started_at=_utc_now_iso(),
            metrics={"stage_timings_ms": {}, "total_duration_ms": 0},
        )
        self._emit(task_id, "case_ingestion", 1, 1, 1, "病例任务已创建")

        try:
            for stage in self.stages:
                stage_started = time.monotonic()
                stage.execute(context, self)
                context.stage_timings_ms[stage.name] = int((time.monotonic() - stage_started) * 1000)

            total_duration_ms = int((time.monotonic() - context.started_monotonic) * 1000)
            result_artifact = self.store.get_artifact(task_id, "result")
            if result_artifact is not None:
                result_data = dict(result_artifact.data)
                timing = dict(result_data.get("timing") or {})
                timing["stage_timings_ms"] = context.stage_timings_ms
                timing["total_duration_ms"] = total_duration_ms
                result_data["timing"] = timing
                context.save_artifact("result", result_data)

            self._emit(
                task_id,
                "completed",
                4,
                6,
                100,
                "任务执行完成",
                {
                    "artifact_types": context.artifact_types,
                    "timing": {
                        "stage_timings_ms": context.stage_timings_ms,
                        "total_duration_ms": total_duration_ms,
                    },
                },
            )
            self.store.update_task(
                task_id,
                status="completed",
                stage="completed",
                progress=100,
                finished_at=_utc_now_iso(),
                error_message=None,
                failure_type=None,
                metrics={"stage_timings_ms": context.stage_timings_ms, "total_duration_ms": total_duration_ms},
            )
        except Exception as exc:
            failure_type = self._classify_failure(exc)
            total_duration_ms = int((time.monotonic() - context.started_monotonic) * 1000)
            self._emit(
                task_id,
                "failed",
                4,
                99,
                min(context.task.progress + 1, 99),
                "任务执行失败",
                {"error": str(exc), "failure_type": failure_type},
            )
            self.store.update_task(
                task_id,
                status="failed",
                stage="failed",
                finished_at=_utc_now_iso(),
                error_message=str(exc),
                failure_type=failure_type,
                metrics={"stage_timings_ms": context.stage_timings_ms, "total_duration_ms": total_duration_ms},
            )

    def _build_context(self, task_id: str) -> TaskExecutionContext:
        task = self.store.get_task(task_id)
        if task is None:
            raise KeyError(f"Unknown task_id: {task_id}")
        case = self.store.get_case(task.case_id)
        if case is None:
            raise KeyError(f"Unknown case_id: {task.case_id}")

        # Keep reading legacy task-scoped config_path if it exists in older records.
        config = ClinicalMvpConfig.load(task.params.get("config_path", self.default_config_path))
        pipeline = build_clinical_mvp_pipeline(config=config)
        patient = PatientProfile(**case.patient_payload)
        manual_phenotypes: list[PhenotypeItem] = []
        for item in case.manual_phenotypes:
            if isinstance(item, dict):
                label = str(item.get("label") or "").strip()
                if label:
                    manual_phenotypes.append(
                        PhenotypeItem(
                            label=label,
                            code=item.get("code"),
                            source=item.get("source") or "task-manual",
                            confidence=item.get("confidence", 1.0),
                            notes=item.get("notes", ""),
                        )
                    )
            elif str(item).strip():
                manual_phenotypes.append(
                    PhenotypeItem(label=str(item).strip(), source="task-manual", confidence=1.0)
                )
        return TaskExecutionContext(
            task_id=task_id,
            task=task,
            case=case,
            store=self.store,
            pipeline=pipeline,
            patient=patient,
            manual_phenotypes=manual_phenotypes,
            top_k=int(task.params.get("top_k", 5)),
            search_depth=task.search_depth,
            started_monotonic=time.monotonic(),
        )

    def _emit(
        self,
        task_id: str,
        step: str,
        task_stage: int,
        seq_in_stage: int,
        progress: int,
        message: str,
        data: dict | None = None,
    ) -> None:
        self.store.append_event(
            build_event_payload(
                task_id=task_id,
                step=step,
                task_stage=task_stage,
                seq_in_stage=seq_in_stage,
                progress=progress,
                message=message,
                data=data,
            )
        )

    def _classify_failure(self, exc: Exception) -> str:
        name = type(exc).__name__.lower()
        message = str(exc).lower()
        if "config" in message or "file not found" in message or name in {"filenotfounderror", "keyerror"}:
            return "configuration_error"
        if "timeout" in message or "connection" in message or "http" in message or "api" in message:
            return "upstream_error"
        return "pipeline_error"
