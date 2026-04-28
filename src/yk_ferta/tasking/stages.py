"""Stage definitions for the task-managed clinical MVP workflow."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from yk_ferta.schemas.clinical import PatientProfile, PhenotypeItem
from yk_ferta.schemas.mvp import ClinicalMvpResponse
from yk_ferta.tasking.models import CaseRecord, TaskRecord
from yk_ferta.tasking.store import SQLiteTaskStore


@dataclass
class TaskExecutionContext:
    """Mutable state shared across workflow stages."""

    task_id: str
    task: TaskRecord
    case: CaseRecord
    store: SQLiteTaskStore
    pipeline: Any
    patient: PatientProfile
    manual_phenotypes: list[PhenotypeItem]
    top_k: int
    search_depth: int
    started_monotonic: float
    stage_timings_ms: dict[str, int] = field(default_factory=dict)
    artifact_types: list[str] = field(default_factory=list)
    review_retry_used: bool = False
    retrieval_base_settings: dict[str, dict[str, int]] = field(default_factory=dict)
    phenotypes: list[PhenotypeItem] = field(default_factory=list)
    phenotype_hints: list[Any] = field(default_factory=list)
    phenotype_tool_runs: list[Any] = field(default_factory=list)
    knowledge_evidence: list[Any] = field(default_factory=list)
    similar_cases: list[Any] = field(default_factory=list)
    initial_candidates: list[Any] = field(default_factory=list)
    normalized_candidates: list[Any] = field(default_factory=list)
    reviews: list[Any] = field(default_factory=list)
    final_recommendation: Any | None = None

    def save_artifact(self, artifact_type: str, data: dict[str, Any]) -> None:
        self.store.save_artifact(task_id=self.task_id, artifact_type=artifact_type, data=data)
        if artifact_type not in self.artifact_types:
            self.artifact_types.append(artifact_type)


@dataclass(frozen=True)
class WorkflowStage:
    """A single executable workflow stage."""

    name: str
    handler: Callable[[TaskExecutionContext, Any], None]

    def execute(self, context: TaskExecutionContext, runner: Any) -> None:
        self.handler(context, runner)


def _extend_unique_evidence(existing: list[Any], extra: list[Any]) -> list[Any]:
    """Append evidence by source_id while keeping the first occurrence."""
    seen = {getattr(item, "source_id", "") for item in existing}
    for item in extra:
        source_id = getattr(item, "source_id", "")
        if source_id and source_id in seen:
            continue
        if source_id:
            seen.add(source_id)
        existing.append(item)
    return existing


def _capture_base_search_settings(context: TaskExecutionContext) -> None:
    if context.retrieval_base_settings:
        return
    component_attrs = {
        "knowledge_searcher": ["web_results", "pubmed_results", "arxiv_results", "wiki_results"],
        "case_searcher": ["public_return_k", "private_return_k", "return_k", "top_n", "vector_top_n"],
        "per_disease_verifier": ["candidate_pubmed_results"],
    }
    for component_name, attr_names in component_attrs.items():
        component = getattr(context.pipeline, component_name, None)
        if component is None:
            continue
        values: dict[str, int] = {}
        for attr_name in attr_names:
            value = getattr(component, attr_name, None)
            if isinstance(value, int):
                values[attr_name] = value
        if values:
            context.retrieval_base_settings[component_name] = values


def _apply_search_depth(context: TaskExecutionContext) -> None:
    _capture_base_search_settings(context)
    depth = max(1, int(context.search_depth))
    for component_name, values in context.retrieval_base_settings.items():
        component = getattr(context.pipeline, component_name, None)
        if component is None:
            continue
        for attr_name, base_value in values.items():
            setattr(component, attr_name, max(base_value, base_value * depth))


def _all_reviews_unsupported(reviews: list[Any]) -> bool:
    return bool(reviews) and all(not bool(getattr(review, "is_supported", False)) for review in reviews)


def _rerun_after_review_failure(context: TaskExecutionContext, runner: Any) -> None:
    if context.review_retry_used or context.search_depth >= 2 or not _all_reviews_unsupported(context.reviews):
        return

    context.review_retry_used = True
    context.search_depth += 1
    context.task.search_depth = context.search_depth
    _apply_search_depth(context)
    context.store.update_task(
        context.task_id,
        stage="search_depth_retry",
        progress=82,
        search_depth=context.search_depth,
    )
    context.save_artifact(
        "retry_control",
        {
            "trigger": "all_reviews_unsupported",
            "search_depth": context.search_depth,
            "review_retry_used": True,
        },
    )
    runner._emit(
        context.task_id,
        "search_depth_retry",
        4,
        4,
        82,
        "逐病复核全部不支持，扩大检索并重试",
        {"search_depth": context.search_depth, "review_count": len(context.reviews)},
    )

    context.knowledge_evidence = context.pipeline.knowledge_searcher.search(context.patient, context.phenotypes)
    context.similar_cases = context.pipeline.case_searcher.search(context.patient, context.phenotypes)
    context.save_artifact(
        "evidence",
        {"knowledge_evidence": [asdict(item) for item in context.knowledge_evidence]},
    )
    context.save_artifact(
        "similar_cases",
        {"similar_cases": [asdict(item) for item in context.similar_cases]},
    )

    context.initial_candidates = context.pipeline.initial_diagnosis_synthesizer.synthesize(
        context.patient,
        context.phenotypes,
        context.phenotype_hints,
        context.knowledge_evidence,
        context.similar_cases,
        context.top_k,
    )
    context.save_artifact(
        "preliminary_diagnosis",
        {"initial_candidates": [asdict(item) for item in context.initial_candidates]},
    )

    context.normalized_candidates = context.pipeline.disease_normalizer.normalize(context.initial_candidates)
    context.save_artifact(
        "normalized_candidates",
        {"normalized_candidates": [asdict(item) for item in context.normalized_candidates]},
    )

    context.reviews = context.pipeline.per_disease_verifier.verify(
        context.patient,
        context.phenotypes,
        context.similar_cases,
        context.knowledge_evidence,
        context.normalized_candidates,
    )
    candidate_evidence = getattr(context.pipeline.per_disease_verifier, "last_candidate_evidence", []) or []
    if candidate_evidence:
        context.knowledge_evidence = _extend_unique_evidence(context.knowledge_evidence, candidate_evidence)
        context.save_artifact(
            "candidate_evidence",
            {"candidate_evidence": [asdict(item) for item in candidate_evidence]},
        )
    context.save_artifact("reviews", {"reviews": [asdict(item) for item in context.reviews]})
    runner._emit(
        context.task_id,
        "search_depth_retry_complete",
        4,
        5,
        84,
        "扩大检索重试完成",
        {
            "search_depth": context.search_depth,
            "candidate_count": len(context.initial_candidates),
            "supported_count": sum(1 for item in context.reviews if getattr(item, "is_supported", False)),
        },
    )


def _run_phenotype_extraction(context: TaskExecutionContext, runner: Any) -> None:
    if context.manual_phenotypes:
        context.phenotypes = context.manual_phenotypes
        runner._emit(
            context.task_id,
            "phenotype_extraction",
            1,
            2,
            10,
            "跳过表型提取，使用手工输入 phenotype",
            {"count": len(context.phenotypes), "mode": "manual"},
        )
    else:
        context.store.update_task(context.task_id, stage="phenotype_extraction", progress=5)
        runner._emit(context.task_id, "phenotype_extraction", 1, 2, 5, "开始表型提取")
        context.phenotypes = context.pipeline.phenotype_extractor.extract(context.patient)
        runner._emit(
            context.task_id,
            "phenotype_extraction",
            1,
            3,
            15,
            "完成表型提取",
            {"count": len(context.phenotypes)},
        )

    context.save_artifact(
        "hpo",
        {"phenotypes": [asdict(item) for item in context.phenotypes]},
    )


def _run_phenotype_analysis(context: TaskExecutionContext, runner: Any) -> None:
    context.store.update_task(context.task_id, stage="phenotype_analysis", progress=20)
    runner._emit(context.task_id, "phenotype_analysis", 2, 1, 20, "开始 phenotype 工具分析")
    context.phenotype_hints, context.phenotype_tool_runs = (
        context.pipeline.phenotype_analyser.analyze_with_details(context.patient, context.phenotypes)
    )
    context.save_artifact(
        "phenotype_hints",
        {
            "phenotype_hints": [asdict(item) for item in context.phenotype_hints],
            "tool_runs": [asdict(item) for item in context.phenotype_tool_runs],
        },
    )
    context.save_artifact(
        "phenotype_tools",
        {"tool_runs": [asdict(item) for item in context.phenotype_tool_runs]},
    )
    runner._emit(
        context.task_id,
        "phenotype_analysis",
        2,
        2,
        28,
        "完成 phenotype 工具分析",
        {
            "count": len(context.phenotype_hints),
            "tools": [
                {
                    "source": item.source,
                    "status": item.status,
                    "candidate_count": len(item.parsed_candidates),
                    "error": item.error,
                }
                for item in context.phenotype_tool_runs
            ],
        },
    )


def _run_parallel_diagnosis(context: TaskExecutionContext, runner: Any) -> None:
    _apply_search_depth(context)
    context.store.update_task(context.task_id, stage="parallel_diagnosis", progress=30)
    runner._emit(context.task_id, "parallel_running", 3, 1, 30, "开始并行证据分析")
    context.knowledge_evidence = context.pipeline.knowledge_searcher.search(context.patient, context.phenotypes)
    context.similar_cases = context.pipeline.case_searcher.search(context.patient, context.phenotypes)
    context.save_artifact(
        "evidence",
        {"knowledge_evidence": [asdict(item) for item in context.knowledge_evidence]},
    )
    context.save_artifact(
        "similar_cases",
        {"similar_cases": [asdict(item) for item in context.similar_cases]},
    )
    runner._emit(
        context.task_id,
        "parallel_complete",
        3,
        2,
        50,
        "并行诊断任务全部完成",
        {
            "knowledge_evidence_count": len(context.knowledge_evidence),
            "similar_case_count": len(context.similar_cases),
        },
    )


def _run_comprehensive_analysis(context: TaskExecutionContext, runner: Any) -> None:
    context.store.update_task(context.task_id, stage="comprehensive_analysis", progress=55)
    runner._emit(context.task_id, "comprehensive_analysis", 3, 3, 55, "开始综合诊断分析")
    context.initial_candidates = context.pipeline.initial_diagnosis_synthesizer.synthesize(
        context.patient,
        context.phenotypes,
        context.phenotype_hints,
        context.knowledge_evidence,
        context.similar_cases,
        context.top_k,
    )
    context.save_artifact(
        "preliminary_diagnosis",
        {"initial_candidates": [asdict(item) for item in context.initial_candidates]},
    )
    runner._emit(
        context.task_id,
        "comprehensive_analysis",
        3,
        4,
        60,
        "综合诊断分析完成",
        {"candidate_count": len(context.initial_candidates)},
    )


def _run_disease_normalization(context: TaskExecutionContext, runner: Any) -> None:
    context.store.update_task(context.task_id, stage="disease_normalization", progress=65)
    runner._emit(context.task_id, "disease_matching", 4, 1, 65, "开始候选疾病标准化")
    context.normalized_candidates = context.pipeline.disease_normalizer.normalize(context.initial_candidates)
    context.save_artifact(
        "normalized_candidates",
        {"normalized_candidates": [asdict(item) for item in context.normalized_candidates]},
    )
    runner._emit(
        context.task_id,
        "disease_matching",
        4,
        2,
        72,
        "完成候选疾病标准化",
        {"count": len(context.normalized_candidates)},
    )


def _run_per_disease_verification(context: TaskExecutionContext, runner: Any) -> None:
    context.store.update_task(context.task_id, stage="per_disease_verification", progress=75)
    runner._emit(context.task_id, "disease_verification", 4, 3, 75, "开始逐病种复核")
    context.reviews = context.pipeline.per_disease_verifier.verify(
        context.patient,
        context.phenotypes,
        context.similar_cases,
        context.knowledge_evidence,
        context.normalized_candidates,
    )
    candidate_evidence = getattr(context.pipeline.per_disease_verifier, "last_candidate_evidence", []) or []
    if candidate_evidence:
        context.knowledge_evidence = _extend_unique_evidence(context.knowledge_evidence, candidate_evidence)
        context.save_artifact(
            "candidate_evidence",
            {"candidate_evidence": [asdict(item) for item in candidate_evidence]},
        )
    context.save_artifact("reviews", {"reviews": [asdict(item) for item in context.reviews]})
    _rerun_after_review_failure(context, runner)
    runner._emit(
        context.task_id,
        "disease_verification",
        4,
        4,
        85,
        "完成逐病种复核",
        {
            "count": len(context.reviews),
            "search_depth": context.search_depth,
            "review_retry_used": context.review_retry_used,
        },
    )


def _run_final_synthesis(context: TaskExecutionContext, runner: Any) -> None:
    context.store.update_task(context.task_id, stage="final_synthesis", progress=90)
    runner._emit(context.task_id, "final_synthesis", 4, 5, 90, "开始最终综合诊断")
    context.final_recommendation = context.pipeline.final_diagnosis_synthesizer.synthesize(
        context.patient,
        context.phenotypes,
        context.phenotype_hints,
        context.knowledge_evidence,
        context.similar_cases,
        context.initial_candidates,
        context.normalized_candidates,
        context.reviews,
    )
    response = ClinicalMvpResponse(
        patient_id=context.patient.patient_id,
        phenotypes=context.phenotypes,
        phenotype_hints=context.phenotype_hints,
        phenotype_tool_runs=context.phenotype_tool_runs,
        knowledge_evidence=context.knowledge_evidence,
        similar_cases=context.similar_cases,
        initial_candidates=context.initial_candidates,
        normalized_candidates=context.normalized_candidates,
        reviews=context.reviews,
        final_recommendation=context.final_recommendation,
        stage_notes={
            **context.pipeline.stage_notes,
            "entry_mode": "manual-phenotypes" if context.manual_phenotypes else "full-note",
            "search_depth": str(context.search_depth),
            "review_retry_used": "true" if context.review_retry_used else "false",
        },
    )
    context.save_artifact(
        "final_report",
        {"final_recommendation": asdict(context.final_recommendation)},
    )
    context.save_artifact(
        "result",
        {
            "response": asdict(response),
            "timing": {
                "stage_timings_ms": context.stage_timings_ms,
            },
        },
    )


def build_default_stages() -> list[WorkflowStage]:
    """Return the default clinical MVP workflow stage registry."""

    return [
        WorkflowStage(name="phenotype_extraction", handler=_run_phenotype_extraction),
        WorkflowStage(name="phenotype_analysis", handler=_run_phenotype_analysis),
        WorkflowStage(name="parallel_diagnosis", handler=_run_parallel_diagnosis),
        WorkflowStage(name="comprehensive_analysis", handler=_run_comprehensive_analysis),
        WorkflowStage(name="disease_normalization", handler=_run_disease_normalization),
        WorkflowStage(name="per_disease_verification", handler=_run_per_disease_verification),
        WorkflowStage(name="final_synthesis", handler=_run_final_synthesis),
    ]
