"""Pydantic schemas for the yk-FERTA HTTP API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str


class ErrorResponse(BaseModel):
    error_code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] | None = None


class ManualPhenotype(BaseModel):
    label: str
    code: str | None = None
    source: str = "manual-review"
    confidence: float | None = 1.0
    notes: str = ""


class CreateCaseRequest(BaseModel):
    case_id: str | None = None
    source: str = "api"
    input_mode: Literal["clinical_note", "phenotype_first"] | None = None
    patient_payload: dict[str, Any]
    manual_phenotypes: list[str | ManualPhenotype] = Field(default_factory=list)


class CaseResponse(BaseModel):
    case_id: str
    source: str
    input_mode: str
    patient_payload: dict[str, Any]
    manual_phenotypes: list[Any]
    created_at: str
    idempotency_key: str = ""


class CreateTaskRequest(BaseModel):
    case_id: str
    top_k: int = 5
    workflow_name: str = "clinical_mvp_v1"


class TaskResponse(BaseModel):
    task_id: str
    case_id: str
    workflow_name: str
    status: str
    stage: str
    progress: int
    search_depth: int
    params: dict[str, Any]
    started_at: str
    finished_at: str
    error_message: str | None = None
    failure_type: str | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = ""


class TaskListResponse(BaseModel):
    tasks: list[TaskResponse]


class CancelTaskResponse(TaskResponse):
    pass


class ArtifactResponse(BaseModel):
    artifact_id: str
    task_id: str
    artifact_type: str
    version: int
    data: dict[str, Any]
    created_at: str


class ArtifactListResponse(BaseModel):
    artifacts: list[ArtifactResponse]


class DiagnosisCardReferenceResponse(BaseModel):
    title: str = ""
    source_type: str = ""
    url: str = ""
    citation: str = ""


class DiagnosisCardResponse(BaseModel):
    disease_name_zh: str = ""
    disease_name_en: str = ""
    clinical_diagnosis: str = ""
    support_level: str = "中"
    confidence: float = 0.0
    omim_id: str = "NA"
    omim_url: str = ""
    orphanet_id: str = "NA"
    orphanet_url: str = ""
    inheritance: str = "NA"
    disease_genes: list[str] = Field(default_factory=list)
    molecular_mechanism: str = "NA"
    pathogenesis: str = ""
    specialties: list[str] = Field(default_factory=list)
    supporting_evidence: list[str] = Field(default_factory=list)
    contradicting_evidence: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    recommended_tests: list[str] = Field(default_factory=list)
    references: list[DiagnosisCardReferenceResponse] = Field(default_factory=list)
    cautions: list[str] = Field(default_factory=list)


class FinalRecommendationResponse(BaseModel):
    summary: str = ""
    candidates: list[Any] = Field(default_factory=list)
    evidence: list[Any] = Field(default_factory=list)
    reviews: list[Any] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)
    cautions: list[str] = Field(default_factory=list)
    diagnosis_cards: list[DiagnosisCardResponse] = Field(default_factory=list)


class ResultResponse(BaseModel):
    response: dict[str, Any]
    timing: dict[str, Any] | None = None


class HpoExtractRequest(BaseModel):
    patient_payload: dict[str, Any]


class HpoExtractResponse(BaseModel):
    phenotypes: list[ManualPhenotype]


class HpoSearchHit(BaseModel):
    code: str
    label: str
    chinese_label: str = ""
    source: str


class HpoSearchResponse(BaseModel):
    query: str
    hits: list[HpoSearchHit]
