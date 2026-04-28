"""Primary traceable diagnostic pipeline for yk-FERTA."""

from __future__ import annotations

from dataclasses import dataclass

from yk_ferta.adapters.base import (
    CandidateGenerator,
    EvidenceRetriever,
    EvidenceVerifier,
    PhenotypeStandardizer,
    TraceableOutputBuilder,
)
from yk_ferta.schemas.pipeline import PipelineRequest, PipelineResponse


@dataclass(slots=True)
class TraceableDiagnosisPipeline:
    """Industrialized counterpart to the DeepRare-style reasoning chain."""

    phenotype_standardizer: PhenotypeStandardizer
    evidence_retriever: EvidenceRetriever
    candidate_generator: CandidateGenerator
    evidence_verifier: EvidenceVerifier
    output_builder: TraceableOutputBuilder

    def run(self, request: PipelineRequest) -> PipelineResponse:
        """Run the full pipeline in a deterministic stage order."""
        phenotypes = self.phenotype_standardizer.standardize(request.patient)
        evidence = self.evidence_retriever.retrieve(request.patient, phenotypes)
        candidates = self.candidate_generator.generate(
            request.patient,
            phenotypes,
            evidence,
            request.top_k,
        )
        reviews = self.evidence_verifier.verify(
            request.patient,
            phenotypes,
            evidence,
            candidates,
        )
        recommendation = self.output_builder.build(
            request.patient,
            phenotypes,
            evidence,
            candidates,
            reviews,
        )
        return PipelineResponse(
            patient_id=request.patient.patient_id,
            phenotypes=phenotypes,
            evidence=evidence,
            candidates=candidates,
            reviews=reviews,
            recommendation=recommendation,
        )
