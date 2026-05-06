import json
import importlib
import time

from fastapi.testclient import TestClient

from yk_ferta.api.app import create_app


def _write_stub_config(path):
    path.write_text(
        """
{
  "openai": {
    "api_key": "",
    "base_url": ""
  },
  "phenotype_extractor": {
    "enabled": false
  },
  "knowledge_searcher": {
    "enabled": false
  },
  "case_searcher": {
    "enabled": false
  }
}
        """.strip(),
        encoding="utf-8",
    )


def _write_auth_config(path):
    path.write_text(
        """
{
  "openai": {
    "api_key": "",
    "base_url": ""
  },
  "auth": {
    "enabled": true,
    "session_secret": "pytest-secret",
    "users": [
      {
        "username": "demo",
        "password": "pass123"
      }
    ]
  },
  "phenotype_extractor": {
    "enabled": false
  },
  "knowledge_searcher": {
    "enabled": false
  },
  "case_searcher": {
    "enabled": false
  }
}
        """.strip(),
        encoding="utf-8",
    )


def test_task_api_can_run_pipeline_and_persist_artifacts(tmp_path):
    db_path = tmp_path / "yk_ferta.sqlite3"
    config_path = tmp_path / "clinical_mvp.json"
    _write_stub_config(config_path)

    app = create_app(db_path=str(db_path), default_config_path=str(config_path))
    client = TestClient(app)

    case_resp = client.post(
        "/api/v1/cases",
        json={
            "source": "pytest",
            "input_mode": "phenotype_first",
            "patient_payload": {
                "patient_id": "svc-001",
                "chief_complaint": "Infertility",
            },
            "manual_phenotypes": ["Female infertility", "Oligomenorrhea"],
        },
    )
    assert case_resp.status_code == 200
    case_id = case_resp.json()["case_id"]

    task_resp = client.post(
        "/api/v1/tasks",
        json={"case_id": case_id, "top_k": 3},
    )
    assert task_resp.status_code == 200
    task_id = task_resp.json()["task_id"]
    assert task_resp.json()["params"] == {"top_k": 3}

    terminal = None
    for _ in range(40):
        task_state = client.get(f"/api/v1/tasks/{task_id}")
        assert task_state.status_code == 200
        terminal = task_state.json()
        if terminal["status"] in {"completed", "failed"}:
            break
        time.sleep(0.1)

    assert terminal is not None
    assert terminal["status"] == "completed"
    assert terminal["failure_type"] is None
    assert "metrics" in terminal
    assert terminal["metrics"]["total_duration_ms"] >= 0
    assert "stage_timings_ms" in terminal["metrics"]
    assert "phenotype_analysis" in terminal["metrics"]["stage_timings_ms"]

    artifacts_resp = client.get(f"/api/v1/tasks/{task_id}/artifacts")
    assert artifacts_resp.status_code == 200
    artifact_types = {item["artifact_type"] for item in artifacts_resp.json()["artifacts"]}
    assert "result" in artifact_types
    assert "hpo" in artifact_types
    assert "phenotype_tools" in artifact_types

    phenotype_tools_resp = client.get(f"/api/v1/tasks/{task_id}/artifacts/phenotype_tools")
    assert phenotype_tools_resp.status_code == 200
    tool_runs = phenotype_tools_resp.json()["data"]["tool_runs"]
    assert tool_runs[0]["source"] == "phenotype-tool-placeholder"
    assert tool_runs[0]["status"] == "success"

    result_resp = client.get(f"/api/v1/tasks/{task_id}/result")
    assert result_resp.status_code == 200
    result_payload = result_resp.json()
    result = result_payload["response"]
    assert result["patient_id"] == "svc-001"
    assert result["stage_notes"]["entry_mode"] == "manual-phenotypes"
    assert result["phenotype_tool_runs"][0]["status"] == "success"
    assert result_payload["timing"]["total_duration_ms"] >= 0
    assert "parallel_diagnosis" in result_payload["timing"]["stage_timings_ms"]
    diagnosis_card = result["final_recommendation"]["diagnosis_cards"][0]
    assert list(diagnosis_card.keys()) == [
        "disease_name_zh",
        "disease_name_en",
        "clinical_diagnosis",
        "support_level",
        "confidence",
        "omim_id",
        "omim_url",
        "orphanet_id",
        "orphanet_url",
        "inheritance",
        "disease_genes",
        "molecular_mechanism",
        "pathogenesis",
        "specialties",
        "supporting_evidence",
        "contradicting_evidence",
        "missing_evidence",
        "recommended_tests",
        "references",
        "cautions",
    ]
    final_report_resp = client.get(f"/api/v1/tasks/{task_id}/artifacts/final_report")
    assert final_report_resp.status_code == 200
    artifact_card = final_report_resp.json()["data"]["final_recommendation"]["diagnosis_cards"][0]
    assert "possible_molecular_subtype" not in artifact_card
    for ref in artifact_card["references"]:
        assert ref["source_type"] in {"pubmed", "omim", "orphanet", "web_search"}


def test_task_api_accepts_reviewed_hpo_objects(tmp_path):
    db_path = tmp_path / "yk_ferta.sqlite3"
    config_path = tmp_path / "clinical_mvp.json"
    _write_stub_config(config_path)

    app = create_app(db_path=str(db_path), default_config_path=str(config_path))
    client = TestClient(app)

    case_resp = client.post(
        "/api/v1/cases",
        json={
            "source": "pytest",
            "input_mode": "phenotype_first",
            "patient_payload": {"patient_id": "svc-hpo-001", "chief_complaint": "不孕不育"},
            "manual_phenotypes": [
                {
                    "label": "Hydatidiform mole",
                    "code": "HP:0032192",
                    "source": "manual-review",
                    "confidence": 1.0,
                    "notes": "葡萄胎",
                }
            ],
        },
    )
    assert case_resp.status_code == 200
    assert case_resp.json()["manual_phenotypes"][0]["code"] == "HP:0032192"

    task_id = client.post(
        "/api/v1/tasks",
        json={"case_id": case_resp.json()["case_id"], "top_k": 1},
    ).json()["task_id"]

    terminal = None
    for _ in range(40):
        task_state = client.get(f"/api/v1/tasks/{task_id}")
        terminal = task_state.json()
        if terminal["status"] in {"completed", "failed"}:
            break
        time.sleep(0.1)

    assert terminal["status"] == "completed"
    result = client.get(f"/api/v1/tasks/{task_id}/result").json()["response"]
    assert result["phenotypes"][0]["code"] == "HP:0032192"


def test_create_case_returns_409_when_case_id_already_exists(tmp_path):
    db_path = tmp_path / "yk_ferta.sqlite3"
    config_path = tmp_path / "clinical_mvp.json"
    _write_stub_config(config_path)

    app = create_app(db_path=str(db_path), default_config_path=str(config_path))
    client = TestClient(app)

    payload = {
        "case_id": "case_demo_duplicate",
        "source": "pytest",
        "input_mode": "clinical_note",
        "patient_payload": {
            "patient_id": "patient_demo_duplicate",
            "chief_complaint": "不孕不育",
        },
        "manual_phenotypes": [],
    }

    first = client.post("/api/v1/cases", json=payload)
    assert first.status_code == 200

    second = client.post("/api/v1/cases", json=payload)
    assert second.status_code == 409
    assert second.json()["error_code"] == "CASE_ALREADY_EXISTS"
    assert second.json()["details"]["case_id"] == "case_demo_duplicate"


def test_module_level_app_reads_runtime_paths_from_env(monkeypatch, tmp_path):
    db_path = tmp_path / "runtime" / "yk_ferta.sqlite3"
    config_path = tmp_path / "config" / "clinical_mvp.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    _write_stub_config(config_path)

    monkeypatch.setenv("YK_FERTA_DB_PATH", str(db_path))
    monkeypatch.setenv("YK_FERTA_CONFIG_PATH", str(config_path))

    import yk_ferta.api.app as app_module

    importlib.reload(app_module)

    assert app_module.app.state.store.db_path == db_path


def test_config_load_allows_deployment_env_overrides(monkeypatch, tmp_path):
    config_path = tmp_path / "clinical_mvp.json"
    config_path.write_text(
        json.dumps(
            {
                "openai": {
                    "api_key": "file-key",
                    "base_url": "https://file.example/v1",
                },
                "phenotype_extractor": {
                    "provider": "rag_hpo",
                    "model_name": "file-extractor-model",
                    "rag_hpo_base_url": "http://127.0.0.1:18080",
                },
                "knowledge_searcher": {"mini_model_name": "file-mini-model"},
                "case_searcher": {"filter_model_name": "file-filter-model"},
                "reasoning": {"model_name": "file-reasoning-model"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("YK_FERTA_OPENAI_API_KEY", "env-key")
    monkeypatch.setenv("YK_FERTA_OPENAI_BASE_URL", "https://env.example/v1")
    monkeypatch.setenv("YK_FERTA_PHENOTYPE_EXTRACTOR_MODEL_NAME", "env-extractor-model")
    monkeypatch.setenv("YK_FERTA_RAG_HPO_BASE_URL", "http://host.docker.internal:18080")
    monkeypatch.setenv("YK_FERTA_KNOWLEDGE_MINI_MODEL_NAME", "env-mini-model")
    monkeypatch.setenv("YK_FERTA_CASE_FILTER_MODEL_NAME", "env-filter-model")
    monkeypatch.setenv("YK_FERTA_REASONING_MODEL_NAME", "env-reasoning-model")

    from yk_ferta.config import ClinicalMvpConfig

    cfg = ClinicalMvpConfig.load(config_path)
    assert cfg.openai.api_key == "env-key"
    assert cfg.openai.base_url == "https://env.example/v1"
    assert cfg.phenotype_extractor.model_name == "env-extractor-model"
    assert cfg.phenotype_extractor.rag_hpo_base_url == "http://host.docker.internal:18080"
    assert cfg.knowledge_searcher.mini_model_name == "env-mini-model"
    assert cfg.case_searcher.filter_model_name == "env-filter-model"
    assert cfg.reasoning.model_name == "env-reasoning-model"


def test_task_events_endpoint_streams_until_done(tmp_path):
    db_path = tmp_path / "yk_ferta.sqlite3"
    config_path = tmp_path / "clinical_mvp.json"
    _write_stub_config(config_path)

    app = create_app(db_path=str(db_path), default_config_path=str(config_path))
    client = TestClient(app)

    case_id = client.post(
        "/api/v1/cases",
        json={
            "source": "pytest",
            "input_mode": "phenotype_first",
            "patient_payload": {"patient_id": "svc-002", "chief_complaint": "Infertility"},
            "manual_phenotypes": ["Female infertility"],
        },
    ).json()["case_id"]
    task_id = client.post(
        "/api/v1/tasks",
        json={"case_id": case_id},
    ).json()["task_id"]

    chunks: list[str] = []
    with client.stream("GET", f"/api/v1/tasks/{task_id}/events") as response:
        assert response.status_code == 200
        for line in response.iter_lines():
            if line:
                chunks.append(line)
            if isinstance(line, str) and "task_all_done" in line:
                break

    joined = "\n".join(chunks)
    assert "case_ingestion" in joined
    assert "completed" in joined or "task_all_done" in joined
    assert "phenotype_analysis" in joined
    done_lines = [line for line in chunks if isinstance(line, str) and '"step": "task_all_done"' in line]
    assert done_lines
    done_payload = json.loads(done_lines[-1].split("data:", 1)[1])
    assert done_payload["task_stage"] == 999
    assert done_payload["seq_in_stage"] == 999
    assert done_payload["ts_ms"] > 0


def test_task_console_page_is_available(tmp_path):
    db_path = tmp_path / "yk_ferta.sqlite3"
    config_path = tmp_path / "clinical_mvp.json"
    _write_stub_config(config_path)

    app = create_app(db_path=str(db_path), default_config_path=str(config_path))
    client = TestClient(app)

    response = client.get("/debug/task-console")
    assert response.status_code == 200
    assert "yk-FERTA 诊断推理控制台" in response.text
    assert "/api/v1/tasks/" in response.text
    assert "临床文本调试模式" in response.text
    assert "present_illness" in response.text
    assert "/api/v1/hpo/extract" in response.text
    assert "/api/v1/hpo/search" in response.text
    assert "表型工具检索" in response.text
    assert "renderPhenotypeToolRuns" in response.text
    assert "PMID：" in response.text


def test_demo_portal_page_is_available(tmp_path):
    db_path = tmp_path / "yk_ferta.sqlite3"
    config_path = tmp_path / "clinical_mvp.json"
    _write_stub_config(config_path)

    app = create_app(db_path=str(db_path), default_config_path=str(config_path))
    client = TestClient(app)

    response = client.get("/demo")
    assert response.status_code == 200
    assert "临床诊断工作台" in response.text
    assert "/debug/case-workbench" in response.text
    assert "/debug/task-viewer" in response.text
    assert "新建病例" in response.text


def test_swagger_ui_and_openapi_are_available(tmp_path):
    db_path = tmp_path / "yk_ferta.sqlite3"
    config_path = tmp_path / "clinical_mvp.json"
    _write_stub_config(config_path)

    app = create_app(db_path=str(db_path), default_config_path=str(config_path))
    client = TestClient(app)

    docs_resp = client.get("/docs")
    assert docs_resp.status_code == 200
    assert "Swagger UI" in docs_resp.text
    assert "yk-FERTA API" in docs_resp.text

    openapi_resp = client.get("/openapi.json")
    assert openapi_resp.status_code == 200
    schema = openapi_resp.json()
    assert schema["info"]["title"] == "yk-FERTA API"
    assert any(tag["name"] == "tasks" for tag in schema["tags"])
    assert "/api/v1/tasks/{task_id}/result" in schema["paths"]


def test_task_viewer_page_contains_workflow_visualization_sections(tmp_path):
    db_path = tmp_path / "yk_ferta.sqlite3"
    config_path = tmp_path / "clinical_mvp.json"
    _write_stub_config(config_path)

    app = create_app(db_path=str(db_path), default_config_path=str(config_path))
    client = TestClient(app)

    response = client.get("/debug/task-viewer")
    assert response.status_code == 200
    assert "工作流可视化" in response.text
    assert "阶段回放" in response.text
    assert "workflowRail" in response.text
    assert "医学文献检索结果" in response.text
    assert "五种可能诊断及支持度" in response.text
    assert "反思判断" in response.text
    assert "私有历史检测案例只支持表型/检测经验参考" in response.text
    assert "PMID：" in response.text


def test_hpo_search_endpoint_returns_local_catalog_hits(tmp_path):
    db_path = tmp_path / "yk_ferta.sqlite3"
    config_path = tmp_path / "clinical_mvp.json"
    _write_stub_config(config_path)

    app = create_app(db_path=str(db_path), default_config_path=str(config_path))
    client = TestClient(app)

    response = client.get("/api/v1/hpo/search", params={"q": "Hydatidiform mole", "limit": 5})
    assert response.status_code == 200
    hits = response.json()["hits"]
    assert any(item["code"] == "HP:0032192" for item in hits)


def test_hpo_extract_endpoint_uses_configured_extractor(tmp_path):
    db_path = tmp_path / "yk_ferta.sqlite3"
    config_path = tmp_path / "clinical_mvp.json"
    _write_stub_config(config_path)

    app = create_app(db_path=str(db_path), default_config_path=str(config_path))
    client = TestClient(app)

    response = client.post(
        "/api/v1/hpo/extract",
        json={
            "patient_payload": {
                "patient_id": "extract-001",
                "chief_complaint": "Infertility",
                "present_illness": "Short narrative.",
            },
        },
    )
    assert response.status_code == 200
    assert response.json()["phenotypes"]


def test_hpo_extract_endpoint_accepts_legacy_patient_payload_keys(tmp_path):
    db_path = tmp_path / "yk_ferta.sqlite3"
    config_path = tmp_path / "clinical_mvp.json"
    _write_stub_config(config_path)

    app = create_app(db_path=str(db_path), default_config_path=str(config_path))
    client = TestClient(app)

    response = client.post(
        "/api/v1/hpo/extract",
        json={
            "patient_payload": {
                "patient_id": "extract-legacy-001",
                "chief_complaint": "Infertility",
                "history_of_present_illness": "Two hydatidiform mole pregnancies.",
                "past_medical_history": "No major past history.",
                "family_history": "Sibling with infertility.",
            },
        },
    )
    assert response.status_code == 200
    assert response.json()["phenotypes"]


def test_hpo_extract_endpoint_runs_blocking_extraction_in_threadpool(tmp_path, monkeypatch):
    db_path = tmp_path / "yk_ferta.sqlite3"
    config_path = tmp_path / "clinical_mvp.json"
    _write_stub_config(config_path)

    app_module = importlib.import_module("yk_ferta.api.app")
    called = {}
    original = app_module.run_in_threadpool

    async def fake_run_in_threadpool(func, *args, **kwargs):
        called["func"] = func
        called["args"] = args
        called["kwargs"] = kwargs
        return await original(func, *args, **kwargs)

    monkeypatch.setattr(app_module, "run_in_threadpool", fake_run_in_threadpool)

    app = create_app(db_path=str(db_path), default_config_path=str(config_path))
    client = TestClient(app)

    response = client.post(
        "/api/v1/hpo/extract",
        json={
            "patient_payload": {
                "patient_id": "extract-threadpool-001",
                "chief_complaint": "Infertility",
                "present_illness": "Short narrative.",
            },
        },
    )

    assert response.status_code == 200
    assert response.json()["phenotypes"]
    assert called["func"] is app_module._extract_hpo_response
    assert called["args"][0]["patient_id"] == "extract-threadpool-001"
    assert called["args"][1] == str(config_path)


def test_create_case_normalizes_legacy_patient_payload_keys(tmp_path):
    db_path = tmp_path / "yk_ferta.sqlite3"
    config_path = tmp_path / "clinical_mvp.json"
    _write_stub_config(config_path)

    app = create_app(db_path=str(db_path), default_config_path=str(config_path))
    client = TestClient(app)

    response = client.post(
        "/api/v1/cases",
        json={
            "source": "pytest",
            "input_mode": "clinical_note",
            "patient_payload": {
                "patient_id": "svc-legacy-001",
                "chief_complaint": "Infertility",
                "history_of_present_illness": "Two hydatidiform mole pregnancies.",
                "past_medical_history": "No major past history.",
                "family_history": "Sibling with infertility.",
            },
        },
    )
    assert response.status_code == 200
    payload = response.json()["patient_payload"]
    assert payload["present_illness"] == "Two hydatidiform mole pregnancies."
    assert "No major past history." in payload["history"]
    assert "家族史：" in payload["history"]


def test_create_case_supports_idempotency_key(tmp_path):
    db_path = tmp_path / "yk_ferta.sqlite3"
    config_path = tmp_path / "clinical_mvp.json"
    _write_stub_config(config_path)

    app = create_app(db_path=str(db_path), default_config_path=str(config_path))
    client = TestClient(app)
    request = {
        "source": "pytest",
        "input_mode": "clinical_note",
        "patient_payload": {
            "patient_id": "svc-idem-case-001",
            "chief_complaint": "Infertility",
            "present_illness": "Short narrative.",
        },
    }
    headers = {"Idempotency-Key": "case-key-001"}

    first = client.post("/api/v1/cases", json=request, headers=headers)
    second = client.post("/api/v1/cases", json=request, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["case_id"] == second.json()["case_id"]
    assert first.json()["idempotency_key"] == "case-key-001"


def test_create_task_supports_idempotency_key(tmp_path):
    db_path = tmp_path / "yk_ferta.sqlite3"
    config_path = tmp_path / "clinical_mvp.json"
    _write_stub_config(config_path)

    app = create_app(db_path=str(db_path), default_config_path=str(config_path))
    client = TestClient(app)
    case_id = client.post(
        "/api/v1/cases",
        json={
            "source": "pytest",
            "input_mode": "phenotype_first",
            "patient_payload": {"patient_id": "svc-idem-task-001", "chief_complaint": "Infertility"},
            "manual_phenotypes": ["Female infertility"],
        },
    ).json()["case_id"]
    headers = {"Idempotency-Key": "task-key-001"}

    first = client.post("/api/v1/tasks", json={"case_id": case_id, "top_k": 2}, headers=headers)
    second = client.post("/api/v1/tasks", json={"case_id": case_id, "top_k": 2}, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["task_id"] == second.json()["task_id"]
    assert first.json()["idempotency_key"] == "task-key-001"


def test_idempotency_key_reuse_with_different_payload_returns_structured_error(tmp_path):
    db_path = tmp_path / "yk_ferta.sqlite3"
    config_path = tmp_path / "clinical_mvp.json"
    _write_stub_config(config_path)

    app = create_app(db_path=str(db_path), default_config_path=str(config_path))
    client = TestClient(app)
    headers = {"Idempotency-Key": "case-key-002"}

    first = client.post(
        "/api/v1/cases",
        json={
            "source": "pytest",
            "input_mode": "clinical_note",
            "patient_payload": {"patient_id": "svc-a", "chief_complaint": "Infertility"},
        },
        headers=headers,
    )
    second = client.post(
        "/api/v1/cases",
        json={
            "source": "pytest",
            "input_mode": "clinical_note",
            "patient_payload": {"patient_id": "svc-b", "chief_complaint": "Amenorrhea"},
        },
        headers=headers,
    )

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["error_code"] == "IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD"
    assert second.json()["retryable"] is False


def test_missing_task_returns_structured_error(tmp_path):
    db_path = tmp_path / "yk_ferta.sqlite3"
    config_path = tmp_path / "clinical_mvp.json"
    _write_stub_config(config_path)

    app = create_app(db_path=str(db_path), default_config_path=str(config_path))
    client = TestClient(app)

    response = client.get("/api/v1/tasks/task_missing")
    assert response.status_code == 404
    assert response.json() == {
        "error_code": "TASK_NOT_FOUND",
        "message": "任务不存在",
        "retryable": False,
        "details": None,
    }


def test_validation_error_returns_structured_error(tmp_path):
    db_path = tmp_path / "yk_ferta.sqlite3"
    config_path = tmp_path / "clinical_mvp.json"
    _write_stub_config(config_path)

    app = create_app(db_path=str(db_path), default_config_path=str(config_path))
    client = TestClient(app)

    response = client.post("/api/v1/tasks", json={"top_k": 2})
    assert response.status_code == 422
    assert response.json()["error_code"] == "INVALID_REQUEST"
    assert response.json()["retryable"] is False
    assert "errors" in response.json()["details"]


def test_auth_redirects_debug_pages_to_login_when_enabled(tmp_path):
    db_path = tmp_path / "yk_ferta.sqlite3"
    config_path = tmp_path / "clinical_mvp.json"
    _write_auth_config(config_path)

    app = create_app(db_path=str(db_path), default_config_path=str(config_path))
    client = TestClient(app, follow_redirects=False)

    response = client.get("/demo", headers={"accept": "text/html"})
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=/demo")


def test_auth_rejects_api_requests_without_login(tmp_path):
    db_path = tmp_path / "yk_ferta.sqlite3"
    config_path = tmp_path / "clinical_mvp.json"
    _write_auth_config(config_path)

    app = create_app(db_path=str(db_path), default_config_path=str(config_path))
    client = TestClient(app)

    response = client.get("/api/v1/hpo/search", params={"q": "Hydatidiform mole"})
    assert response.status_code == 401
    assert response.json()["error_code"] == "AUTH_REQUIRED"


def test_auth_login_allows_debug_pages_and_api(tmp_path):
    db_path = tmp_path / "yk_ferta.sqlite3"
    config_path = tmp_path / "clinical_mvp.json"
    _write_auth_config(config_path)

    app = create_app(db_path=str(db_path), default_config_path=str(config_path))
    client = TestClient(app, follow_redirects=False)

    login_page = client.get("/login")
    assert login_page.status_code == 200
    assert "内网调试面板登录" in login_page.text

    login = client.post(
        "/login",
        data={"username": "demo", "password": "pass123", "next": "/demo"},
    )
    assert login.status_code == 303
    assert login.headers["location"] == "/demo"
    assert "yk_ferta_session" in login.headers.get("set-cookie", "")

    cookies = login.cookies
    demo = client.get("/demo", cookies=cookies)
    assert demo.status_code == 200
    assert "临床诊断工作台" in demo.text

    api = client.get(
        "/api/v1/hpo/search",
        params={"q": "Hydatidiform mole", "limit": 5},
        cookies=cookies,
    )
    assert api.status_code == 200
    assert any(item["code"] == "HP:0032192" for item in api.json()["hits"])
