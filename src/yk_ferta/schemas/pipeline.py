"""Pipeline request/response models."""

from __future__ import annotations

from dataclasses import dataclass, field

from .clinical import CandidateCondition, PatientProfile, PhenotypeItem
from .evidence import CandidateReview, EvidenceItem, TraceableRecommendation


@dataclass(slots=True)
class PipelineRequest:
    """Top-level request for the traceable diagnostic pipeline."""

    patient: PatientProfile
    top_k: int = 5
    context: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class PipelineResponse:
    """Top-level response for the traceable diagnostic pipeline."""

    patient_id: str
    phenotypes: list[PhenotypeItem]
    evidence: list[EvidenceItem]
    candidates: list[CandidateCondition]
    reviews: list[CandidateReview]
    recommendation: TraceableRecommendation
