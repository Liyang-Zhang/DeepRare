"""Adapter protocols for pluggable yk-FERTA components."""

from __future__ import annotations

from typing import Protocol

from yk_ferta.schemas.clinical import CandidateCondition, PatientProfile, PhenotypeItem
from yk_ferta.schemas.evidence import CandidateReview, EvidenceItem, TraceableRecommendation


class PhenotypeStandardizer(Protocol):
    """Convert raw patient information into normalized phenotypes."""

    def standardize(self, patient: PatientProfile) -> list[PhenotypeItem]:
        """Return normalized phenotypes."""


class EvidenceRetriever(Protocol):
    """Fetch evidence for a patient and phenotype set."""

    def retrieve(
        self,
        patient: PatientProfile,
        phenotypes: list[PhenotypeItem],
    ) -> list[EvidenceItem]:
        """Return evidence items from configured knowledge sources."""


class CandidateGenerator(Protocol):
    """Produce candidate etiologies or diagnoses from standardized inputs."""

    def generate(
        self,
        patient: PatientProfile,
        phenotypes: list[PhenotypeItem],
        evidence: list[EvidenceItem],
        top_k: int,
    ) -> list[CandidateCondition]:
        """Return ranked candidates."""


class EvidenceVerifier(Protocol):
    """Verify each candidate against retrieved evidence."""

    def verify(
        self,
        patient: PatientProfile,
        phenotypes: list[PhenotypeItem],
        evidence: list[EvidenceItem],
        candidates: list[CandidateCondition],
    ) -> list[CandidateReview]:
        """Return per-candidate review results."""


class TraceableOutputBuilder(Protocol):
    """Build physician-facing structured output."""

    def build(
        self,
        patient: PatientProfile,
        phenotypes: list[PhenotypeItem],
        evidence: list[EvidenceItem],
        candidates: list[CandidateCondition],
        reviews: list[CandidateReview],
    ) -> TraceableRecommendation:
        """Return final traceable recommendation."""
