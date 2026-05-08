"""Minimal FastAPI service for task-managed clinical MVP execution."""

from __future__ import annotations

import asyncio
import base64
import html
import hashlib
import hmac
import json
import os
import sqlite3
import urllib.parse
from time import time
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, Header, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

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
    FrozenResultResponse,
    FinalRecommendationResponse,
    HealthResponse,
    HpoExtractRequest,
    HpoExtractResponse,
    HpoSearchHit,
    HpoSearchResponse,
    ManualPhenotype,
    ResultBodyResponse,
    TaskEventResponse,
    TaskListResponse,
    TaskResponse,
)
from yk_ferta.agents.factory import build_clinical_mvp_pipeline
from yk_ferta.config import AuthConfig, ClinicalMvpConfig
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


def _extract_hpo_response(patient_payload: dict, config_path: str) -> HpoExtractResponse:
    """Run blocking phenotype extraction outside the event loop."""
    config = ClinicalMvpConfig.load(config_path)
    pipeline = build_clinical_mvp_pipeline(config=config)
    patient = PatientProfile(**_normalize_patient_payload(patient_payload))
    phenotypes = pipeline.phenotype_extractor.extract(patient)
    return HpoExtractResponse(
        phenotypes=[
            ManualPhenotype(
                label=item.label,
                chinese_label=item.chinese_label,
                code=item.code,
                source=item.source,
                confidence=item.confidence,
                notes=item.notes,
            )
            for item in phenotypes
        ]
    )


def _sign_session(username: str, secret: str) -> str:
    payload = base64.urlsafe_b64encode(username.encode("utf-8")).decode("ascii").rstrip("=")
    signature = hmac.new(secret.encode("utf-8"), payload.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def _verify_session(token: str, secret: str) -> str | None:
    try:
        payload, signature = token.split(".", 1)
    except ValueError:
        return None
    expected = hmac.new(secret.encode("utf-8"), payload.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None
    padding = "=" * (-len(payload) % 4)
    try:
        return base64.urlsafe_b64decode(f"{payload}{padding}").decode("utf-8").strip() or None
    except Exception:
        return None


def _is_html_request(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    return "text/html" in accept.lower()


def _render_login_page(next_path: str, error_message: str = "") -> HTMLResponse:
    escaped_next = html.escape(next_path or "/demo", quote=True)
    error_block = (
        f'<div class="error">{error_message}</div>'
        if error_message
        else '<div class="hint">请使用分配的账号登录后访问调试面板。</div>'
    )
    page = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>亿康不孕不育辅助诊断智能体登录</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f3efe7;
      --card: #fffdfa;
      --text: #21303b;
      --muted: #697784;
      --accent: #1f6b5b;
      --accent-strong: #11463b;
      --line: #d9d2c5;
      --danger: #b6473e;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      background:
        radial-gradient(circle at top right, rgba(31,107,91,0.16), transparent 34%),
        linear-gradient(135deg, #f6f2ea, var(--bg));
      color: var(--text);
      font-family: "Segoe UI", "PingFang SC", "Helvetica Neue", sans-serif;
    }}
    .panel {{
      width: min(480px, calc(100vw - 32px));
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 20px;
      padding: 28px;
      box-shadow: 0 24px 64px rgba(17, 34, 51, 0.12);
    }}
    .eyebrow {{
      margin: 0 0 8px;
      color: var(--accent-strong);
      font-size: 13px;
      font-weight: 700;
      letter-spacing: 0.04em;
    }}
    h1 {{
      margin: 0 0 8px;
      font-size: 28px;
      line-height: 1.2;
      word-break: keep-all;
    }}
    p {{ margin: 0 0 18px; color: var(--muted); line-height: 1.5; }}
    label {{ display: block; margin: 14px 0 6px; font-size: 14px; color: var(--muted); }}
    input {{
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 12px 14px;
      font-size: 15px;
      background: #fff;
    }}
    button {{
      width: 100%;
      margin-top: 18px;
      border: 0;
      border-radius: 12px;
      padding: 13px 16px;
      background: var(--accent);
      color: #fff;
      font-size: 15px;
      font-weight: 600;
      cursor: pointer;
    }}
    button:hover {{ background: var(--accent-strong); }}
    .error {{
      margin-bottom: 14px;
      padding: 10px 12px;
      border-radius: 10px;
      background: rgba(182,71,62,0.08);
      color: var(--danger);
      font-size: 14px;
    }}
    .hint {{
      margin-bottom: 14px;
      padding: 10px 12px;
      border-radius: 10px;
      background: rgba(31,107,91,0.08);
      color: var(--accent-strong);
      font-size: 14px;
    }}
  </style>
</head>
<body>
  <main class="panel">
    <div class="eyebrow">亿康不孕不育辅助诊断智能体</div>
    <h1>智能体登录</h1>
    <p>登录后可访问病例录入、任务执行、结果查看与追溯页面。</p>
    {error_block}
    <form method="post" action="/login">
      <input type="hidden" name="next" value="{escaped_next}">
      <label for="username">账号</label>
      <input id="username" name="username" autocomplete="username" required>
      <label for="password">密码</label>
      <input id="password" name="password" type="password" autocomplete="current-password" required>
      <button type="submit">登录并继续</button>
    </form>
  </main>
</body>
</html>
"""
    return HTMLResponse(page)


def _should_protect_path(path: str) -> bool:
    protected_prefixes = ("/api/v1/", "/debug/", "/docs", "/redoc")
    protected_exact = {"/demo", "/openapi.json"}
    return path.startswith(protected_prefixes) or path in protected_exact


def _apply_no_store(response) -> None:
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"


def _parse_login_form(body: bytes) -> tuple[str, str, str]:
    parsed = urllib.parse.parse_qs(body.decode("utf-8"), keep_blank_values=True)
    username = (parsed.get("username", [""])[0] or "").strip()
    password = parsed.get("password", [""])[0] or ""
    next_path = (parsed.get("next", ["/demo"])[0] or "/demo").strip() or "/demo"
    return username, password, next_path


def _normalize_diagnosis_card(card: dict) -> DiagnosisCardResponse:
    return DiagnosisCardResponse(
        rank=int(card.get("rank", 0) or 0),
        diagnosis_match_score=float(card.get("diagnosis_match_score", 0.0) or 0.0),
        diagnosis_match_percent=int(card.get("diagnosis_match_percent", 0) or 0),
        disease_name_zh=str(card.get("disease_name_zh", "") or ""),
        disease_name_en=str(card.get("disease_name_en", "") or ""),
        clinical_diagnosis=str(card.get("clinical_diagnosis", "") or ""),
        support_level=str(card.get("support_level", "中") or "中"),
        confidence=float(card.get("confidence", 0.0) or 0.0),
        ranking_reason=str(card.get("ranking_reason", "") or ""),
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


def _freeze_result_contract(payload: dict) -> FrozenResultResponse:
    response = dict(payload.get("response") or {})
    final_recommendation = dict(response.get("final_recommendation") or {})
    normalized_cards = [
        _normalize_diagnosis_card(item).model_dump()
        for item in final_recommendation.get("diagnosis_cards", [])
        if isinstance(item, dict)
    ]
    frozen_response = ResultBodyResponse(
        patient_id=str(response.get("patient_id", "") or ""),
        phenotypes=response.get("phenotypes", []) or [],
        phenotype_hints=response.get("phenotype_hints", []) or [],
        phenotype_tool_runs=response.get("phenotype_tool_runs", []) or [],
        knowledge_evidence=response.get("knowledge_evidence", []) or [],
        similar_cases=response.get("similar_cases", []) or [],
        initial_candidates=response.get("initial_candidates", []) or [],
        normalized_candidates=response.get("normalized_candidates", []) or [],
        reviews=response.get("reviews", []) or [],
        stage_notes={
            str(key): str(value)
            for key, value in (response.get("stage_notes", {}) or {}).items()
        },
        final_recommendation=FinalRecommendationResponse(
        summary=str(final_recommendation.get("summary", "") or ""),
        candidates=final_recommendation.get("candidates", []) or [],
        evidence=final_recommendation.get("evidence", []) or [],
        reviews=final_recommendation.get("reviews", []) or [],
        next_steps=[str(item) for item in final_recommendation.get("next_steps", []) if str(item).strip()],
        cautions=[str(item) for item in final_recommendation.get("cautions", []) if str(item).strip()],
        final_diagnosis_confidence=float(final_recommendation.get("final_diagnosis_confidence", 0.0) or 0.0),
        final_diagnosis_confidence_percent=int(final_recommendation.get("final_diagnosis_confidence_percent", 0) or 0),
        diagnosis_cards=[DiagnosisCardResponse(**item) for item in normalized_cards],
        ),
    )
    return FrozenResultResponse(response=frozen_response, timing=payload.get("timing"))


def create_app(
    *,
    db_path: str = "data/yk_ferta.sqlite3",
    default_config_path: str = "config/clinical_mvp.json",
) -> FastAPI:
    startup_config = ClinicalMvpConfig.load(default_config_path)
    auth_config = startup_config.auth
    app = FastAPI(
        title="亿康不孕不育辅助诊断智能体 API",
        version="0.1.0",
        description=(
            "亿康不孕不育辅助诊断智能体服务接口。\n\n"
            "用途：病例创建、任务执行、SSE 进度订阅、结果获取、追溯 artifact 查询。\n\n"
            "说明：`/api/v1/tasks/{task_id}/result` 是正式前端主消费接口；"
            "`/artifacts` 主要用于追溯与调试。"
        ),
        openapi_tags=[
            {"name": "system", "description": "健康检查与服务元数据"},
            {"name": "hpo", "description": "HPO 提取与检索接口"},
            {"name": "cases", "description": "病例创建与查询接口"},
            {"name": "tasks", "description": "任务创建、状态查询、取消接口"},
            {"name": "results", "description": "正式结果与追溯产物接口"},
            {"name": "events", "description": "SSE 进度事件流接口"},
            {"name": "debug-ui", "description": "研发/演示静态页面入口"},
        ],
    )
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

    def _is_authenticated(request: Request) -> bool:
        if not auth_config.enabled:
            return True
        token = request.cookies.get(auth_config.session_cookie_name, "")
        username = _verify_session(token, auth_config.session_secret)
        if not username:
            return False
        return any(user.username == username for user in auth_config.users)

    def _login_success_response(next_path: str, username: str) -> RedirectResponse:
        destination = next_path if next_path.startswith("/") else "/demo"
        response = RedirectResponse(destination, status_code=status.HTTP_303_SEE_OTHER)
        response.set_cookie(
            auth_config.session_cookie_name,
            _sign_session(username, auth_config.session_secret),
            max_age=auth_config.session_max_age_seconds,
            httponly=True,
            samesite="lax",
            path="/",
        )
        _apply_no_store(response)
        return response

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        if not auth_config.enabled:
            return await call_next(request)
        path = request.url.path
        if path in {"/healthz", "/login", "/logout"}:
            return await call_next(request)
        if not _should_protect_path(path):
            return await call_next(request)
        if _is_authenticated(request):
            response = await call_next(request)
            if request.url.path in {"/login", "/logout"} or _should_protect_path(request.url.path):
                _apply_no_store(response)
            return response
        if _is_html_request(request):
            next_path = request.url.path
            if request.url.query:
                next_path = f"{next_path}?{request.url.query}"
            login_url = f"/login?next={urllib.parse.quote(next_path, safe='/?=&')}"
            response = RedirectResponse(login_url, status_code=status.HTTP_303_SEE_OTHER)
            _apply_no_store(response)
            return response
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content=_error_payload(
                "AUTH_REQUIRED",
                "请先登录后访问该资源",
                False,
                {"login_path": "/login"},
            ),
        )

    @app.get("/healthz", response_model=HealthResponse, tags=["system"], summary="健康检查")
    def healthz() -> HealthResponse:
        return HealthResponse(status="ok")

    @app.get("/login", include_in_schema=False)
    async def login_page(next: str = "/demo") -> HTMLResponse:
        if not auth_config.enabled:
            return HTMLResponse("<html><body><p>Auth disabled.</p></body></html>")
        response = _render_login_page(next)
        _apply_no_store(response)
        return response

    @app.post("/login", include_in_schema=False)
    async def login_submit(request: Request):
        if not auth_config.enabled:
            return RedirectResponse("/demo", status_code=status.HTTP_303_SEE_OTHER)
        body = await request.body()
        username, password, next_path = _parse_login_form(body)
        if any(user.username == username and user.password == password for user in auth_config.users):
            return _login_success_response(next_path, username)
        response = _render_login_page(next_path, "账号或密码错误。")
        _apply_no_store(response)
        return response

    @app.post("/logout", include_in_schema=False)
    async def logout() -> RedirectResponse:
        response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
        if auth_config.enabled:
            response.delete_cookie(auth_config.session_cookie_name, path="/")
        _apply_no_store(response)
        return response

    @app.get("/demo", tags=["debug-ui"], summary="演示入口页")
    async def demo_portal() -> FileResponse:
        return FileResponse(demo_portal_ui_path)

    @app.get("/debug/task-console", tags=["debug-ui"], summary="研发控制台页")
    async def task_console() -> FileResponse:
        return FileResponse(debug_ui_path)

    @app.get("/debug/case-workbench", tags=["debug-ui"], summary="病例录入与 HPO 确认页")
    async def case_workbench() -> FileResponse:
        return FileResponse(case_workbench_ui_path)

    @app.get("/debug/task-viewer", tags=["debug-ui"], summary="任务执行与结果页")
    async def task_viewer() -> FileResponse:
        return FileResponse(task_viewer_ui_path)

    @app.post(
        "/api/v1/hpo/extract",
        response_model=HpoExtractResponse,
        tags=["hpo"],
        summary="提取病例中的 HPO/表型",
    )
    async def extract_hpo(payload: HpoExtractRequest) -> HpoExtractResponse:
        return await run_in_threadpool(
            _extract_hpo_response,
            payload.patient_payload,
            default_config_path,
        )

    @app.get(
        "/api/v1/hpo/search",
        response_model=HpoSearchResponse,
        tags=["hpo"],
        summary="搜索 HPO 候选词条",
    )
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

    @app.post(
        "/api/v1/cases",
        response_model=CaseResponse,
        tags=["cases"],
        summary="创建病例",
    )
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

        try:
            record = store.create_case(
                case_id=payload.case_id,
                source=payload.source,
                input_mode=input_mode,
                patient_payload=patient_payload,
                manual_phenotypes=manual_phenotypes,
                idempotency_key=idempotency_key or "",
            )
        except sqlite3.IntegrityError as exc:
            if "cases.case_id" in str(exc):
                raise ApiError(
                    status_code=status.HTTP_409_CONFLICT,
                    error_code="CASE_ALREADY_EXISTS",
                    message="病例 ID 已存在，请更换 case_id 或省略该字段由服务自动生成",
                    retryable=False,
                    details={"case_id": payload.case_id or ""},
                ) from exc
            raise
        if idempotency_key:
            store.save_idempotency_record(
                endpoint="/api/v1/cases",
                idempotency_key=idempotency_key,
                request_fingerprint=_request_fingerprint(request_payload),
                resource_type="case",
                resource_id=record.case_id,
            )
        return CaseResponse(**asdict(record))

    @app.get(
        "/api/v1/cases/{case_id}",
        response_model=CaseResponse,
        tags=["cases"],
        summary="查询病例",
    )
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

    @app.post(
        "/api/v1/tasks",
        response_model=TaskResponse,
        tags=["tasks"],
        summary="创建任务",
    )
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

    @app.get(
        "/api/v1/tasks",
        response_model=TaskListResponse,
        tags=["tasks"],
        summary="列出任务",
    )
    async def list_tasks(case_id: str | None = Query(default=None)) -> TaskListResponse:
        return TaskListResponse(tasks=[TaskResponse(**asdict(item)) for item in store.list_tasks(case_id=case_id)])

    @app.get(
        "/api/v1/tasks/{task_id}",
        response_model=TaskResponse,
        tags=["tasks"],
        summary="查询任务状态",
    )
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

    @app.post(
        "/api/v1/tasks/{task_id}/cancel",
        response_model=CancelTaskResponse,
        tags=["tasks"],
        summary="取消任务",
    )
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

    @app.get(
        "/api/v1/tasks/{task_id}/artifacts",
        response_model=ArtifactListResponse,
        tags=["results"],
        summary="列出任务产物",
    )
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

    @app.get(
        "/api/v1/tasks/{task_id}/artifacts/{artifact_type}",
        response_model=ArtifactResponse,
        tags=["results"],
        summary="查询单个任务产物",
    )
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

    @app.get(
        "/api/v1/tasks/{task_id}/result",
        response_model=FrozenResultResponse,
        tags=["results"],
        summary="获取正式结果",
        description="正式前端应优先消费该接口；artifact 仅用于追溯与调试。",
    )
    async def get_result(task_id: str) -> FrozenResultResponse:
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

    @app.get(
        "/api/v1/tasks/{task_id}/events",
        tags=["events"],
        summary="订阅任务 SSE 事件流",
        description="支持 `Last-Event-ID` 断线重连；终态会发送 `event:done`。",
    )
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
                    payload = TaskEventResponse(
                        task_id=event.task_id,
                        step=event.step,
                        task_stage=event.task_stage,
                        seq_in_stage=event.seq_in_stage,
                        progress=event.progress,
                        message=event.message,
                        ts_ms=event.ts_ms,
                        data=event.data,
                    ).model_dump()
                    yield f"id:{event.event_id}\ndata:{json.dumps(payload, ensure_ascii=False)}\n\n"

                record = store.get_task(task_id)
                if record is None:
                    break
                if record.status in {"completed", "failed", "cancelled"} and not events:
                    terminal_payload = TaskEventResponse(
                        task_id=record.task_id,
                        step="task_all_done",
                        progress=record.progress,
                        task_stage=999,
                        seq_in_stage=999,
                        message=record.status,
                        ts_ms=int(time() * 1000),
                        data={},
                    ).model_dump()
                    yield f"event:done\ndata:{json.dumps(terminal_payload, ensure_ascii=False)}\n\n"
                    break
                if await request.is_disconnected():
                    break
                yield ": keep-alive\n\n"
                await asyncio.sleep(0.5)

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    return app


app = create_app(
    db_path=os.getenv("YK_FERTA_DB_PATH", "data/yk_ferta.sqlite3"),
    default_config_path=os.getenv("YK_FERTA_CONFIG_PATH", "config/clinical_mvp.json"),
)
