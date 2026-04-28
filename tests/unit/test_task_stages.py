import time
from types import SimpleNamespace

from yk_ferta.schemas.clinical import CandidateCondition, PatientProfile, PhenotypeItem
from yk_ferta.schemas.evidence import CandidateReview, EvidenceItem
from yk_ferta.schemas.mvp import NormalizedDisease, SimilarCase
from yk_ferta.tasking.stages import (
    TaskExecutionContext,
    _run_comprehensive_analysis,
    _run_disease_normalization,
    _run_parallel_diagnosis,
    _run_per_disease_verification,
    build_default_stages,
)
from yk_ferta.tasking.store import SQLiteTaskStore


def test_default_task_stage_order_is_stable():
    stages = build_default_stages()
    assert [stage.name for stage in stages] == [
        "phenotype_extraction",
        "phenotype_analysis",
        "parallel_diagnosis",
        "comprehensive_analysis",
        "disease_normalization",
        "per_disease_verification",
        "final_synthesis",
    ]


def test_all_unsupported_reviews_trigger_one_deeper_retry(tmp_path):
    class FakeKnowledgeSearcher:
        web_results = 2
        pubmed_results = 2
        arxiv_results = 0
        wiki_results = 0

        def search(self, patient, phenotypes):
            return [
                EvidenceItem(
                    source_id=f"evidence-depth-{self.pubmed_results}",
                    source_type="pubmed",
                    title="demo",
                    summary="demo",
                )
            ]

    class FakeCaseSearcher:
        public_return_k = 1
        private_return_k = 1
        vector_top_n = 10

        def search(self, patient, phenotypes):
            return [
                SimilarCase(
                    case_id=f"case-depth-{self.public_return_k}",
                    source="public-case-bank:test",
                    summary="summary",
                    diagnosis="Demo diagnosis",
                    score=0.8,
                )
            ]

    class FakeInitialDiagnosisSynthesizer:
        def synthesize(self, patient, phenotypes, phenotype_hints, knowledge_evidence, similar_cases, top_k):
            return [
                CandidateCondition(
                    name=f"Candidate depth {knowledge_evidence[0].source_id}",
                    rank=1,
                    score=0.9,
                    rationale="demo",
                )
            ]

    class FakeDiseaseNormalizer:
        def normalize(self, candidates):
            return [
                NormalizedDisease(
                    original_name=candidates[0].name,
                    normalized_name=candidates[0].name,
                    disease_id="ORPHA:1",
                    ontology="Orphanet",
                    mapping_score=0.9,
                )
            ]

    class FakePerDiseaseVerifier:
        candidate_pubmed_results = 2

        def __init__(self):
            self.last_candidate_evidence = []

        def verify(self, patient, phenotypes, similar_cases, knowledge_evidence, normalized_candidates):
            supported = self.candidate_pubmed_results > 2
            self.last_candidate_evidence = [
                EvidenceItem(
                    source_id=f"candidate-pubmed-depth-{self.candidate_pubmed_results}",
                    source_type="pubmed",
                    title="candidate",
                    summary="candidate",
                )
            ]
            return [
                CandidateReview(
                    candidate_name=normalized_candidates[0].normalized_name,
                    is_supported=supported,
                    confidence=0.8 if supported else 0.1,
                    reasoning="demo",
                )
            ]

    store = SQLiteTaskStore(str(tmp_path / "yk_ferta.sqlite3"))
    case = store.create_case(
        case_id=None,
        source="pytest",
        input_mode="phenotype_first",
        patient_payload={"patient_id": "case-1", "chief_complaint": "Infertility"},
        manual_phenotypes=[],
    )
    task = store.create_task(case_id=case.case_id, params={"top_k": 5})

    pipeline = SimpleNamespace(
        knowledge_searcher=FakeKnowledgeSearcher(),
        case_searcher=FakeCaseSearcher(),
        initial_diagnosis_synthesizer=FakeInitialDiagnosisSynthesizer(),
        disease_normalizer=FakeDiseaseNormalizer(),
        per_disease_verifier=FakePerDiseaseVerifier(),
        stage_notes={},
    )
    context = TaskExecutionContext(
        task_id=task.task_id,
        task=task,
        case=case,
        store=store,
        pipeline=pipeline,
        patient=PatientProfile(**case.patient_payload),
        manual_phenotypes=[],
        top_k=5,
        search_depth=1,
        started_monotonic=time.monotonic(),
        phenotypes=[PhenotypeItem(label="Infertility", code="HP:0000789")],
    )

    emitted = []

    class FakeRunner:
        def _emit(self, task_id, step, task_stage, seq_in_stage, progress, message, data=None):
            emitted.append((step, progress, data or {}))

    runner = FakeRunner()

    _run_parallel_diagnosis(context, runner)
    _run_comprehensive_analysis(context, runner)
    _run_disease_normalization(context, runner)
    _run_per_disease_verification(context, runner)

    assert context.review_retry_used is True
    assert context.search_depth == 2
    assert context.reviews[0].is_supported is True
    assert pipeline.knowledge_searcher.pubmed_results == 4
    assert pipeline.case_searcher.public_return_k == 2
    assert pipeline.per_disease_verifier.candidate_pubmed_results == 4
    assert any(step == "search_depth_retry" for step, _, _ in emitted)
    assert any(step == "search_depth_retry_complete" for step, _, _ in emitted)
    persisted = store.get_task(task.task_id)
    assert persisted is not None
    assert persisted.search_depth == 2
