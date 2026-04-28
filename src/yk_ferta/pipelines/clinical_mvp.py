"""DeepRare-style clinical MVP pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field

from yk_ferta.adapters.clinical_mvp import (
    ClinicalCaseSearcher,
    ClinicalKnowledgeSearcher,
    ClinicalPhenotypeAnalyser,
    ClinicalPhenotypeExtractor,
    DiseaseNormalizer,
    FinalDiagnosisSynthesizer,
    InitialDiagnosisSynthesizer,
    PerDiseaseVerifier,
)
from yk_ferta.schemas.mvp import ClinicalMvpRequest, ClinicalMvpResponse


def _extend_unique_evidence(existing: list, extra: list) -> list:
    """Append evidence items by source_id while preserving order."""
    seen = {getattr(item, "source_id", "") for item in existing}
    for item in extra:
        source_id = getattr(item, "source_id", "")
        if source_id and source_id in seen:
            continue
        if source_id:
            seen.add(source_id)
        existing.append(item)
    return existing


@dataclass(slots=True)
class ClinicalMvpPipeline:
    """Minimal clinical-only pipeline that mirrors the DeepRare stage order."""

    phenotype_extractor: ClinicalPhenotypeExtractor
    phenotype_analyser: ClinicalPhenotypeAnalyser
    knowledge_searcher: ClinicalKnowledgeSearcher
    case_searcher: ClinicalCaseSearcher
    initial_diagnosis_synthesizer: InitialDiagnosisSynthesizer
    disease_normalizer: DiseaseNormalizer
    per_disease_verifier: PerDiseaseVerifier
    final_diagnosis_synthesizer: FinalDiagnosisSynthesizer
    stage_notes: dict[str, str] = field(default_factory=dict)

    def run(self, request: ClinicalMvpRequest) -> ClinicalMvpResponse:
        """Run the DeepRare-style clinical-only MVP."""
        patient = request.patient
        phenotypes = self.phenotype_extractor.extract(patient)
        phenotype_hints, phenotype_tool_runs = self.phenotype_analyser.analyze_with_details(patient, phenotypes)
        knowledge_evidence = self.knowledge_searcher.search(patient, phenotypes)
        similar_cases = self.case_searcher.search(patient, phenotypes)
        initial_candidates = self.initial_diagnosis_synthesizer.synthesize(
            patient,
            phenotypes,
            phenotype_hints,
            knowledge_evidence,
            similar_cases,
            request.top_k,
        )
        normalized_candidates = self.disease_normalizer.normalize(initial_candidates)
        reviews = self.per_disease_verifier.verify(
            patient,
            phenotypes,
            similar_cases,
            knowledge_evidence,
            normalized_candidates,
        )
        candidate_evidence = getattr(self.per_disease_verifier, "last_candidate_evidence", []) or []
        knowledge_evidence = _extend_unique_evidence(knowledge_evidence, candidate_evidence)
        final_recommendation = self.final_diagnosis_synthesizer.synthesize(
            patient,
            phenotypes,
            phenotype_hints,
            knowledge_evidence,
            similar_cases,
            initial_candidates,
            normalized_candidates,
            reviews,
        )
        return ClinicalMvpResponse(
            patient_id=patient.patient_id,
            phenotypes=phenotypes,
            phenotype_hints=phenotype_hints,
            phenotype_tool_runs=phenotype_tool_runs,
            knowledge_evidence=knowledge_evidence,
            similar_cases=similar_cases,
            initial_candidates=initial_candidates,
            normalized_candidates=normalized_candidates,
            reviews=reviews,
            final_recommendation=final_recommendation,
            stage_notes=self.stage_notes.copy(),
        )
