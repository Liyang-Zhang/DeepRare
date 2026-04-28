"""DeepRare-style clinical MVP schemas."""

from __future__ import annotations

from dataclasses import dataclass, field

from .clinical import CandidateCondition, PatientProfile, PhenotypeItem
from .evidence import CandidateReview, EvidenceItem, TraceableRecommendation


@dataclass(slots=True)
class PhenotypeToolHit:
    """Candidate hint returned by phenotype-driven tools."""

    source: str
    disease_name: str
    disease_id: str | None = None
    score: float | None = None
    notes: str = ""


@dataclass(slots=True)
class PhenotypeToolRun:
    """Execution detail for one phenotype-driven external tool."""

    source: str
    status: str
    query: list[str] = field(default_factory=list)
    raw_result: str = ""
    parsed_candidates: list[PhenotypeToolHit] = field(default_factory=list)
    error: str = ""
    elapsed_ms: int | None = None


@dataclass(slots=True)
class SimilarCase:
    """Retrieved similar case from the local case bank."""

    case_id: str
    source: str
    summary: str
    diagnosis: str
    score: float | None = None
    evidence_role: str = "diagnosis_reference"
    disease_id: str = ""
    reported_genes: list[str] = field(default_factory=list)
    phenotype_relevant_genes: list[str] = field(default_factory=list)
    variant_summary: str = ""
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class NormalizedDisease:
    """Standardized disease entity for verification."""

    original_name: str
    normalized_name: str
    disease_id: str | None = None
    ontology: str = ""
    mapping_score: float | None = None
    normalization_top_matches: list[dict[str, object]] = field(default_factory=list)
    normalization_decision_source: str = ""
    normalization_decision_reason: str = ""
    normalization_decision_confidence: float | None = None


@dataclass(slots=True)
class ClinicalMvpRequest:
    """Clinical-note-only request for the MVP pipeline."""

    patient: PatientProfile
    top_k: int = 5


@dataclass(slots=True)
class ClinicalMvpResponse:
    """DeepRare-style stage outputs for the clinical MVP."""

    patient_id: str
    phenotypes: list[PhenotypeItem]
    phenotype_hints: list[PhenotypeToolHit]
    phenotype_tool_runs: list[PhenotypeToolRun]
    knowledge_evidence: list[EvidenceItem]
    similar_cases: list[SimilarCase]
    initial_candidates: list[CandidateCondition]
    normalized_candidates: list[NormalizedDisease]
    reviews: list[CandidateReview]
    final_recommendation: TraceableRecommendation
    stage_notes: dict[str, str] = field(default_factory=dict)
