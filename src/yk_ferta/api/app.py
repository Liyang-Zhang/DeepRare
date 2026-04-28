"""Minimal FastAPI service for task-managed clinical MVP execution."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, Header, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from yk_ferta.api.schemas import (
    ArtifactListResponse,
    ArtifactResponse,
    CancelTaskResponse,
    CaseResponse,
    CreateCaseRequest,
    CreateTaskRequest,
    DiagnosisCardReferenceResponse,
    DiagnosisCardResponse,
    ErrorResponse,
    FinalRecommendationResponse,
    HealthResponse,
    HpoExtractRequest,
    HpoExtractResponse,
    HpoSearchHit,
    HpoSearchResponse,
    ManualPhenotype,
    ResultResponse,
    TaskListResponse,
    TaskResponse,
)
from yk_ferta.agents.factory import build_clinical_mvp_pipeline
from yk_ferta.config import ClinicalMvpConfig
from yk_ferta.schemas.clinical import PatientProfile
from yk_ferta.services.hpo_catalog import search_hpo_catalog
from yk_ferta.tasking.runner import ClinicalMvpTaskRunner
from yk_ferta.tasking.store import SQLiteTaskStore


class ApiError(Exception):
    def __init__(
        self,
        *,
        status_code: int,
        error_code: str,
        message: str,
        retryable: bool = False,
        details: dict | None = None,
    ) -> None:
        self.status_code = status_code
        self.error_code = error_code
        self.message = message
        self.retryable = retryable
        self.details = details or {}
        super().__init__(message)


def _normalize_patient_payload(payload: dict) -> dict:
    """Accept legacy API field names and map them to PatientProfile fields."""
    normalized = dict(payload)

    legacy_aliases = {
        "history_of_present_illness": "present_illness",
        "past_medical_history": "history",
    }
    for old_key, new_key in legacy_aliases.items():
        if normalized.get(old_key) and not normalized.get(new_key):
            normalized[new_key] = normalized[old_key]
        normalized.pop(old_key, None)

    family_history = str(normalized.pop("family_history", "") or "").strip()
    if family_history:
        history = str(normalized.get("history", "") or "").strip()
        normalized["history"] = (
            f"{history}\n家族史：{family_history}" if history else f"家族史：{family_history}"
        )

    return normalized


def _error_payload(error_code: str, message: str, retryable: bool, details: dict | None = None) -> dict:
    return ErrorResponse(
        error_code=error_code,
        message=message,
        retryable=retryable,
        details=details or None,
    ).model_dump()


def _request_fingerprint(payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _normalize_diagnosis_card(card: dict) -> DiagnosisCardResponse:
    return DiagnosisCardResponse(
        disease_name_zh=str(card.get("disease_name_zh", "") or ""),
        disease_name_en=str(card.get("disease_name_en", "") or ""),
        clinical_diagnosis=str(card.get("clinical_diagnosis", "") or ""),
        support_level=str(card.get("support_level", "中") or "中"),
        confidence=float(card.get("confidence", 0.0) or 0.0),
        omim_id=str(card.get("omim_id", "NA") or "NA"),
        omim_url=str(card.get("omim_url", "") or ""),
        orphanet_id=str(card.get("orphanet_id", "NA") or "NA"),
        orphanet_url=str(card.get("orphanet_url", "") or ""),
        inheritance=str(card.get("inheritance", "NA") or "NA"),
        disease_genes=[str(item) for item in card.get("disease_genes", []) if str(item).strip()],
        molecular_mechanism=str(card.get("molecular_mechanism", "NA") or "NA"),
        pathogenesis=str(card.get("pathogenesis", "") or ""),
        specialties=[str(item) for item in card.get("specialties", []) if str(item).strip()],
        supporting_evidence=[str(item) for item in card.get("supporting_evidence", []) if str(item).strip()],
        contradicting_evidence=[str(item) for item in card.get("contradicting_evidence", []) if str(item).strip()],
        missing_evidence=[str(item) for item in card.get("missing_evidence", []) if str(item).strip()],
        recommended_tests=[str(item) for item in card.get("recommended_tests", []) if str(item).strip()],
        references=[
            DiagnosisCardReferenceResponse(
                title=str(ref.get("title", "") or ""),
                source_type=str(ref.get("source_type", "") or ""),
                url=str(ref.get("url", "") or ""),
                citation=str(ref.get("citation", "") or ""),
            )
            for ref in card.get("references", [])
            if isinstance(ref, dict)
        ],
        cautions=[str(item) for item in card.get("cautions", []) if str(item).strip()],
    )


def _freeze_result_contract(payload: dict) -> ResultResponse:
    response = dict(payload.get("response") or {})
    final_recommendation = dict(response.get("final_recommendation") or {})
    normalized_cards = [
        _normalize_diagnosis_card(item).model_dump()
        for item in final_recommendation.get("diagnosis_cards", [])
        if isinstance(item, dict)
    ]
    response["final_recommendation"] = FinalRecommendationResponse(
        summary=str(final_recommendation.get("summary", "") or ""),
        candidates=final_recommendation.get("candidates", []) or [],
        evidence=final_recommendation.get("evidence", []) or [],
        reviews=final_recommendation.get("reviews", []) or [],
        next_steps=[str(item) for item in final_recommendation.get("next_steps", []) if str(item).strip()],
        cautions=[str(item) for item in final_recommendation.get("cautions", []) if str(item).strip()],
        diagnosis_cards=[DiagnosisCardResponse(**item) for item in normalized_cards],
    ).model_dump()
    return ResultResponse(response=response, timing=payload.get("timing"))


def create_app(
    *,
    db_path: str = "data/yk_ferta.sqlite3",
    default_config_path: str = "config/clinical_mvp.json",
) -> FastAPI:
    app = FastAPI(title="yk-FERTA API", version="0.1.0")
    store = SQLiteTaskStore(db_path)
    runner = ClinicalMvpTaskRunner(store, default_config_path=default_config_path)

    app.state.store = store
    app.state.runner = runner

    @app.exception_handler(ApiError)
    async def api_error_handler(_: Request, exc: ApiError):
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_payload(exc.error_code, exc.message, exc.retryable, exc.details),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content=_error_payload(
                "INVALID_REQUEST",
                "请求参数或请求体不合法",
                False,
                {"errors": exc.errors()},
            ),
        )

    static_dir = Path(__file__).resolve().parent / "static"
    demo_portal_ui_path = static_dir / "demo_portal.html"
    debug_ui_path = static_dir / "task_console.html"
    case_workbench_ui_path = static_dir / "case_workbench.html"
    task_viewer_ui_path = static_dir / "task_viewer.html"

    @app.get("/healthz", response_model=HealthResponse)
    def healthz() -> HealthResponse:
        return HealthResponse(status="ok")

    @app.get("/demo")
    async def demo_portal() -> FileResponse:
        return FileResponse(demo_portal_ui_path)

    @app.get("/debug/task-console")
    async def task_console() -> FileResponse:
        return FileResponse(debug_ui_path)

    @app.get("/debug/case-workbench")
    async def case_workbench() -> FileResponse:
        return FileResponse(case_workbench_ui_path)

    @app.get("/debug/task-viewer")
    async def task_viewer() -> FileResponse:
        return FileResponse(task_viewer_ui_path)

    @app.post("/api/v1/hpo/extract", response_model=HpoExtractResponse)
    async def extract_hpo(payload: HpoExtractRequest) -> HpoExtractResponse:
        config = ClinicalMvpConfig.load(default_config_path)
        pipeline = build_clinical_mvp_pipeline(config=config)
        patient = PatientProfile(**_normalize_patient_payload(payload.patient_payload))
        phenotypes = pipeline.phenotype_extractor.extract(patient)
        return HpoExtractResponse(
            phenotypes=[
                ManualPhenotype(
                    label=item.label,
                    code=item.code,
                    source=item.source,
                    confidence=item.confidence,
                    notes=item.notes,
                )
                for item in phenotypes
            ]
        )

    @app.get("/api/v1/hpo/search", response_model=HpoSearchResponse)
    async def search_hpo(q: str = Query(..., min_length=1), limit: int = Query(default=20, ge=1, le=50)) -> HpoSearchResponse:
        hits = search_hpo_catalog(q, limit=limit)
        return HpoSearchResponse(
            query=q,
            hits=[
                HpoSearchHit(
                    code=item.code,
                    label=item.label,
                    chinese_label=item.chinese_label,
                    source=item.source,
                )
                for item in hits
            ],
        )

    @app.post("/api/v1/cases", response_model=CaseResponse)
    async def create_case(
        payload: CreateCaseRequest,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> CaseResponse:
        patient_payload = _normalize_patient_payload(payload.patient_payload)
        patient_payload.setdefault("patient_id", payload.case_id or patient_payload.get("patient_id") or "")
        if not patient_payload.get("patient_id"):
            patient_payload["patient_id"] = f"case_input_{id(patient_payload)}"
        input_mode = payload.input_mode or (
            "phenotype_first" if payload.manual_phenotypes else "clinical_note"
        )
        manual_phenotypes = [
            item.model_dump() if hasattr(item, "model_dump") else item
            for item in payload.manual_phenotypes
        ]
        request_payload = {
            "case_id": payload.case_id,
            "source": payload.source,
            "input_mode": input_mode,
            "patient_payload": patient_payload,
            "manual_phenotypes": manual_phenotypes,
        }
        if idempotency_key:
            fingerprint = _request_fingerprint(request_payload)
            existing_key = store.get_idempotency_record("/api/v1/cases", idempotency_key)
            if existing_key is not None:
                if existing_key.request_fingerprint != fingerprint:
                    raise ApiError(
                        status_code=status.HTTP_409_CONFLICT,
                        error_code="IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD",
                        message="同一个 Idempotency-Key 不能用于不同的创建病例请求",
                        retryable=False,
                    )
                existing = store.get_case(existing_key.resource_id)
                if existing is None:
                    raise ApiError(
                        status_code=status.HTTP_409_CONFLICT,
                        error_code="IDEMPOTENCY_RESOURCE_MISSING",
                        message="幂等键已存在，但关联病例不存在",
                        retryable=False,
                    )
                return CaseResponse(**asdict(existing))

        record = store.create_case(
            case_id=payload.case_id,
            source=payload.source,
            input_mode=input_mode,
            patient_payload=patient_payload,
            manual_phenotypes=manual_phenotypes,
            idempotency_key=idempotency_key or "",
        )
        if idempotency_key:
            store.save_idempotency_record(
                endpoint="/api/v1/cases",
                idempotency_key=idempotency_key,
                request_fingerprint=_request_fingerprint(request_payload),
                resource_type="case",
                resource_id=record.case_id,
            )
        return CaseResponse(**asdict(record))

    @app.get("/api/v1/cases/{case_id}", response_model=CaseResponse)
    async def get_case(case_id: str) -> CaseResponse:
        record = store.get_case(case_id)
        if record is None:
            raise ApiError(
                status_code=status.HTTP_404_NOT_FOUND,
                error_code="CASE_NOT_FOUND",
                message="病例不存在",
                retryable=False,
            )
        return CaseResponse(**asdict(record))

    @app.post("/api/v1/tasks", response_model=TaskResponse)
    async def create_task(
        payload: CreateTaskRequest,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> TaskResponse:
        if store.get_case(payload.case_id) is None:
            raise ApiError(
                status_code=status.HTTP_404_NOT_FOUND,
                error_code="CASE_NOT_FOUND",
                message="病例不存在，无法创建任务",
                retryable=False,
            )
        request_payload = {
            "case_id": payload.case_id,
            "top_k": int(payload.top_k),
            "workflow_name": payload.workflow_name,
        }
        if idempotency_key:
            fingerprint = _request_fingerprint(request_payload)
            existing_key = store.get_idempotency_record("/api/v1/tasks", idempotency_key)
            if existing_key is not None:
                if existing_key.request_fingerprint != fingerprint:
                    raise ApiError(
                        status_code=status.HTTP_409_CONFLICT,
                        error_code="IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD",
                        message="同一个 Idempotency-Key 不能用于不同的创建任务请求",
                        retryable=False,
                    )
                existing = store.get_task(existing_key.resource_id)
                if existing is None:
                    raise ApiError(
                        status_code=status.HTTP_409_CONFLICT,
                        error_code="IDEMPOTENCY_RESOURCE_MISSING",
                        message="幂等键已存在，但关联任务不存在",
                        retryable=False,
                    )
                return TaskResponse(**asdict(existing))

        record = store.create_task(
            case_id=payload.case_id,
            workflow_name=payload.workflow_name,
            params={
                "top_k": int(payload.top_k),
            },
            idempotency_key=idempotency_key or "",
        )
        if idempotency_key:
            store.save_idempotency_record(
                endpoint="/api/v1/tasks",
                idempotency_key=idempotency_key,
                request_fingerprint=_request_fingerprint(request_payload),
                resource_type="task",
                resource_id=record.task_id,
            )
        runner.start(record.task_id)
        return TaskResponse(**asdict(record))

    @app.get("/api/v1/tasks", response_model=TaskListResponse)
    async def list_tasks(case_id: str | None = Query(default=None)) -> TaskListResponse:
        return TaskListResponse(tasks=[TaskResponse(**asdict(item)) for item in store.list_tasks(case_id=case_id)])

    @app.get("/api/v1/tasks/{task_id}", response_model=TaskResponse)
    async def get_task(task_id: str) -> TaskResponse:
        record = store.get_task(task_id)
        if record is None:
            raise ApiError(
                status_code=status.HTTP_404_NOT_FOUND,
                error_code="TASK_NOT_FOUND",
                message="任务不存在",
                retryable=False,
            )
        return TaskResponse(**asdict(record))

    @app.post("/api/v1/tasks/{task_id}/cancel", response_model=CancelTaskResponse)
    async def cancel_task(task_id: str) -> CancelTaskResponse:
        record = store.get_task(task_id)
        if record is None:
            raise ApiError(
                status_code=status.HTTP_404_NOT_FOUND,
                error_code="TASK_NOT_FOUND",
                message="任务不存在",
                retryable=False,
            )
        if record.status in {"completed", "failed", "cancelled"}:
            return CancelTaskResponse(**asdict(record))
        store.update_task(task_id, status="cancelled", stage="cancelled", finished_at="")
        return CancelTaskResponse(**asdict(store.get_task(task_id)))

    @app.get("/api/v1/tasks/{task_id}/artifacts", response_model=ArtifactListResponse)
    async def list_artifacts(task_id: str) -> ArtifactListResponse:
        if store.get_task(task_id) is None:
            raise ApiError(
                status_code=status.HTTP_404_NOT_FOUND,
                error_code="TASK_NOT_FOUND",
                message="任务不存在",
                retryable=False,
            )
        return ArtifactListResponse(
            artifacts=[ArtifactResponse(**asdict(item)) for item in store.list_artifacts(task_id)]
        )

    @app.get("/api/v1/tasks/{task_id}/artifacts/{artifact_type}", response_model=ArtifactResponse)
    async def get_artifact(task_id: str, artifact_type: str) -> ArtifactResponse:
        if store.get_task(task_id) is None:
            raise ApiError(
                status_code=status.HTTP_404_NOT_FOUND,
                error_code="TASK_NOT_FOUND",
                message="任务不存在",
                retryable=False,
            )
        artifact = store.get_artifact(task_id, artifact_type)
        if artifact is None:
            raise ApiError(
                status_code=status.HTTP_404_NOT_FOUND,
                error_code="ARTIFACT_NOT_FOUND",
                message="指定产物不存在",
                retryable=False,
                details={"artifact_type": artifact_type},
            )
        return ArtifactResponse(**asdict(artifact))

    @app.get("/api/v1/tasks/{task_id}/result", response_model=ResultResponse)
    async def get_result(task_id: str) -> ResultResponse:
        if store.get_task(task_id) is None:
            raise ApiError(
                status_code=status.HTTP_404_NOT_FOUND,
                error_code="TASK_NOT_FOUND",
                message="任务不存在",
                retryable=False,
            )
        artifact = store.get_artifact(task_id, "result")
        if artifact is None:
            raise ApiError(
                status_code=status.HTTP_404_NOT_FOUND,
                error_code="TASK_RESULT_NOT_READY",
                message="任务结果尚未生成",
                retryable=True,
            )
        return _freeze_result_contract(artifact.data)

    @app.get("/api/v1/tasks/{task_id}/events")
    async def stream_events(
        task_id: str,
        request: Request,
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        if store.get_task(task_id) is None:
            raise ApiError(
                status_code=status.HTTP_404_NOT_FOUND,
                error_code="TASK_NOT_FOUND",
                message="任务不存在",
                retryable=False,
            )

        async def event_stream():
            cursor = int(last_event_id or "0")
            while True:
                events = store.list_events(task_id, after_event_id=cursor)
                for event in events:
                    cursor = event.event_id
                    payload = {
                        "task_id": event.task_id,
                        "step": event.step,
                        "task_stage": event.task_stage,
                        "seq_in_stage": event.seq_in_stage,
                        "progress": event.progress,
                        "message": event.message,
                        "ts_ms": event.ts_ms,
                        "data": event.data,
                    }
                    yield f"id:{event.event_id}\ndata:{json.dumps(payload, ensure_ascii=False)}\n\n"

                record = store.get_task(task_id)
                if record is None:
                    break
                if record.status in {"completed", "failed", "cancelled"} and not events:
                    terminal_payload = {
                        "task_id": record.task_id,
                        "step": "task_all_done",
                        "progress": record.progress,
                        "task_stage": 999,
                        "seq_in_stage": 999,
                        "message": record.status,
                        "data": {},
                    }
                    yield f"event:done\ndata:{json.dumps(terminal_payload, ensure_ascii=False)}\n\n"
                    break
                if await request.is_disconnected():
                    break
                yield ": keep-alive\n\n"
                await asyncio.sleep(0.5)

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    return app


app = create_app()
