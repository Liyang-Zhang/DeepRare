"""Evidence and output domain models."""

from __future__ import annotations

from dataclasses import dataclass, field

from .clinical import CandidateCondition


@dataclass(slots=True)
class EvidenceItem:
    """Single evidence unit retrieved from a knowledge source."""

    source_id: str
    source_type: str
    title: str
    summary: str
    citation: str = ""
    url: str = ""
    relevance_score: float | None = None
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class CandidateReview:
    """Verification result for a candidate condition."""

    candidate_name: str
    is_supported: bool
    confidence: float | None = None
    reasoning: str = ""
    evidence_ids: list[str] = field(default_factory=list)
    supporting_evidence: list[str] = field(default_factory=list)
    contradicting_evidence: list[str] = field(default_factory=list)
    missing_evidence: list[str] = field(default_factory=list)


@dataclass(slots=True)
class TraceableRecommendation:
    """Final structured output for physician-facing recommendation."""

    summary: str
    candidates: list[CandidateCondition]
    evidence: list[EvidenceItem]
    reviews: list[CandidateReview]
    next_steps: list[str] = field(default_factory=list)
    cautions: list[str] = field(default_factory=list)
    diagnosis_cards: list[dict[str, object]] = field(default_factory=list)
