"""DeepRare-style stage adapters for the clinical MVP."""

from __future__ import annotations

from typing import Protocol

from yk_ferta.schemas.clinical import CandidateCondition, PatientProfile, PhenotypeItem
from yk_ferta.schemas.evidence import CandidateReview, EvidenceItem, TraceableRecommendation
from yk_ferta.schemas.mvp import NormalizedDisease, PhenotypeToolHit, PhenotypeToolRun, SimilarCase


class ClinicalPhenotypeExtractor(Protocol):
    """Extract normalized phenotypes from clinical text."""

    def extract(self, patient: PatientProfile) -> list[PhenotypeItem]:
        """Return normalized phenotype items."""


class ClinicalPhenotypeAnalyser(Protocol):
    """Fetch phenotype-driven candidate disease hints."""

    def analyze(
        self,
        patient: PatientProfile,
        phenotypes: list[PhenotypeItem],
    ) -> list[PhenotypeToolHit]:
        """Return candidate hints from phenotype tools."""

    def analyze_with_details(
        self,
        patient: PatientProfile,
        phenotypes: list[PhenotypeItem],
    ) -> tuple[list[PhenotypeToolHit], list[PhenotypeToolRun]]:
        """Return candidate hints plus per-tool execution details."""


class ClinicalKnowledgeSearcher(Protocol):
    """Retrieve knowledge evidence from configured sources."""

    def search(
        self,
        patient: PatientProfile,
        phenotypes: list[PhenotypeItem],
    ) -> list[EvidenceItem]:
        """Return knowledge evidence for the current case."""


class ClinicalCaseSearcher(Protocol):
    """Retrieve similar local cases for the current patient."""

    def search(
        self,
        patient: PatientProfile,
        phenotypes: list[PhenotypeItem],
    ) -> list[SimilarCase]:
        """Return similar local cases."""


class InitialDiagnosisSynthesizer(Protocol):
    """Generate first-round candidate conditions from aggregated evidence."""

    def synthesize(
        self,
        patient: PatientProfile,
        phenotypes: list[PhenotypeItem],
        phenotype_hints: list[PhenotypeToolHit],
        knowledge_evidence: list[EvidenceItem],
        similar_cases: list[SimilarCase],
        top_k: int,
    ) -> list[CandidateCondition]:
        """Return first-round ranked candidates."""


class DiseaseNormalizer(Protocol):
    """Map free-text disease names to normalized entities."""

    def normalize(
        self,
        candidates: list[CandidateCondition],
    ) -> list[NormalizedDisease]:
        """Return normalized disease entities."""


class PerDiseaseVerifier(Protocol):
    """Review each normalized candidate against accumulated evidence."""

    def verify(
        self,
        patient: PatientProfile,
        phenotypes: list[PhenotypeItem],
        similar_cases: list[SimilarCase],
        knowledge_evidence: list[EvidenceItem],
        normalized_candidates: list[NormalizedDisease],
    ) -> list[CandidateReview]:
        """Return per-candidate review results."""


class FinalDiagnosisSynthesizer(Protocol):
    """Build the final physician-facing recommendation."""

    def synthesize(
        self,
        patient: PatientProfile,
        phenotypes: list[PhenotypeItem],
        phenotype_hints: list[PhenotypeToolHit],
        knowledge_evidence: list[EvidenceItem],
        similar_cases: list[SimilarCase],
        initial_candidates: list[CandidateCondition],
        normalized_candidates: list[NormalizedDisease],
        reviews: list[CandidateReview],
    ) -> TraceableRecommendation:
        """Return the final traceable recommendation."""
