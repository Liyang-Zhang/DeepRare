"""Run the yk-FERTA clinical-text benchmark on public fertility cases."""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
import statistics
import time
import traceback
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
os.chdir(REPO_ROOT)

from yk_ferta.agents.factory import build_clinical_mvp_pipeline
from yk_ferta.config import ClinicalMvpConfig
from yk_ferta.schemas.clinical import PatientProfile
from yk_ferta.schemas.mvp import ClinicalMvpRequest, ClinicalMvpResponse


def _extend_unique_evidence(existing: list, extra: list) -> list:
    seen = {getattr(item, "source_id", "") for item in existing}
    for item in extra:
        source_id = getattr(item, "source_id", "")
        if source_id and source_id in seen:
            continue
        if source_id:
            seen.add(source_id)
        existing.append(item)
    return existing


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run-public-case-benchmark",
        description=(
            "Run the yk-FERTA clinical-text benchmark against "
            "database/fertility_public_cases_rds.csv."
        ),
    )
    parser.add_argument(
        "--config",
        default="config/clinical_mvp.json",
        help="Path to the clinical MVP JSON config.",
    )
    parser.add_argument(
        "--input",
        default="database/fertility_public_cases_rds.csv",
        help="Path to fertility_public_cases_rds.csv.",
    )
    parser.add_argument(
        "--output-dir",
        default="",
        help=(
            "Directory for benchmark outputs. Defaults to "
            "outputs/benchmark_runs/<timestamp>_public_cases."
        ),
    )
    parser.add_argument("--top-k", type=int, default=5, help="Top-k candidates to keep.")
    parser.add_argument("--limit", type=int, default=0, help="Optional maximum number of cases to run.")
    parser.add_argument("--offset", type=int, default=0, help="Optional starting offset within the filtered rows.")
    parser.add_argument(
        "--tiers",
        default="strong,moderate,weak",
        help="Comma-separated fertility relevance tiers to include. Default: strong,moderate,weak",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume an existing run by skipping already completed benchmark_case_id values.",
    )
    return parser


def _timestamp_slug() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def normalize_orpha_id(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    digits = raw.split(":")[-1].strip()
    if not digits:
        return ""
    return f"ORPHA:{digits}"


def row_to_patient_profile(row: dict[str, str]) -> PatientProfile:
    benchmark_case_id = (row.get("_id") or "").strip() or "benchmark-case-unknown"
    case_text = (row.get("case_report") or "").strip()
    return PatientProfile(
        patient_id=benchmark_case_id,
        raw_note=case_text,
        metadata={
            "source_dataset": (row.get("source_dataset") or "").strip(),
            "source_record_id": (row.get("source_record_id") or row.get("_id") or "").strip(),
            "source_pmid": (row.get("source_pmid") or "").strip(),
            "gold_diagnosis": (row.get("diagnosis") or "").strip(),
            "gold_orpha_name": (row.get("Orpha_name") or "").strip(),
            "gold_orpha_id": normalize_orpha_id(row.get("Orpha_id") or ""),
        },
    )


def load_benchmark_rows(
    path: str | Path,
    *,
    tiers: set[str] | None = None,
    offset: int = 0,
    limit: int = 0,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    with Path(path).open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            if not (row.get("case_report") or "").strip():
                continue
            if not (row.get("diagnosis") or "").strip():
                continue
            if not (row.get("Orpha_name") or "").strip():
                continue
            if not normalize_orpha_id(row.get("Orpha_id") or ""):
                continue
            if tiers and (row.get("fertility_relevance_tier") or "").strip() not in tiers:
                continue
            rows.append(row)
    if offset > 0:
        rows = rows[offset:]
    if limit > 0:
        rows = rows[:limit]
    return rows


def top_orpha_ids(response: ClinicalMvpResponse) -> list[str]:
    ids: list[str] = []
    for card in response.final_recommendation.diagnosis_cards:
        value = normalize_orpha_id(str(card.get("orphanet_id", "")))
        if value and value not in ids:
            ids.append(value)
    return ids


def top_diagnosis_names(response: ClinicalMvpResponse) -> list[str]:
    names: list[str] = []
    for card in response.final_recommendation.diagnosis_cards:
        for key in ("clinical_diagnosis", "disease_name_zh", "disease_name_en"):
            value = str(card.get(key, "")).strip()
            if value:
                names.append(value)
                break
    return names


def evaluate_case_result(
    row: dict[str, str],
    response: ClinicalMvpResponse,
    elapsed_ms: int,
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    gold_orpha_id = normalize_orpha_id(row.get("Orpha_id") or "")
    predicted_orpha_ids = top_orpha_ids(response)
    predicted_names = top_diagnosis_names(response)
    top1_orpha_id = predicted_orpha_ids[0] if predicted_orpha_ids else ""
    top1_name = predicted_names[0] if predicted_names else ""
    top1_hit = bool(gold_orpha_id and top1_orpha_id == gold_orpha_id)
    top3_hit = bool(gold_orpha_id and gold_orpha_id in predicted_orpha_ids[:3])
    top5_hit = bool(gold_orpha_id and gold_orpha_id in predicted_orpha_ids[:5])
    diagnostics = diagnostics or {}
    return {
        "benchmark_case_id": (row.get("_id") or "").strip(),
        "source_dataset": (row.get("source_dataset") or "").strip(),
        "source_record_id": (row.get("source_record_id") or row.get("_id") or "").strip(),
        "source_pmid": (row.get("source_pmid") or "").strip(),
        "fertility_relevance_tier": (row.get("fertility_relevance_tier") or "").strip(),
        "gold_diagnosis_text": (row.get("diagnosis") or "").strip(),
        "gold_orpha_name": (row.get("Orpha_name") or "").strip(),
        "gold_orpha_id": gold_orpha_id,
        "status": "completed",
        "elapsed_ms": elapsed_ms,
        "top1_name": top1_name,
        "top1_orpha_id": top1_orpha_id,
        "predicted_orpha_ids": predicted_orpha_ids,
        "predicted_diagnosis_names": predicted_names,
        "top1_hit": top1_hit,
        "top3_hit": top3_hit,
        "top5_hit": top5_hit,
        "phenotype_count": len(response.phenotypes),
        "normalized_candidate_count": len(response.normalized_candidates),
        "review_supported_count": sum(1 for item in response.reviews if item.is_supported),
        "final_diagnosis_confidence_percent": response.final_recommendation.final_diagnosis_confidence_percent,
        "stage_timings_ms": diagnostics.get("stage_timings_ms", {}),
        "fallback_flags": diagnostics.get("fallback_flags", {}),
        "error": "",
    }


def summarize_results(case_results: list[dict[str, Any]]) -> dict[str, Any]:
    completed = [item for item in case_results if item.get("status") == "completed"]
    failed = [item for item in case_results if item.get("status") == "failed"]
    elapsed = [int(item["elapsed_ms"]) for item in completed if item.get("elapsed_ms") is not None]
    confidence = [
        int(item["final_diagnosis_confidence_percent"])
        for item in completed
        if item.get("final_diagnosis_confidence_percent") is not None
    ]

    def _rate(key: str) -> float:
        if not completed:
            return 0.0
        hits = sum(1 for item in completed if item.get(key))
        return round(hits / len(completed), 4)

    def _mean(values: list[int]) -> float:
        if not values:
            return 0.0
        return round(sum(values) / len(values), 2)

    def _percentile(values: list[int], pct: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        if len(ordered) == 1:
            return float(ordered[0])
        index = (len(ordered) - 1) * pct
        lower = math.floor(index)
        upper = math.ceil(index)
        if lower == upper:
            return float(ordered[int(index)])
        lower_value = ordered[lower]
        upper_value = ordered[upper]
        return round(lower_value + (upper_value - lower_value) * (index - lower), 2)

    return {
        "total_cases": len(case_results),
        "completed_cases": len(completed),
        "failed_cases": len(failed),
        "workflow_success_rate": round(len(completed) / len(case_results), 4) if case_results else 0.0,
        "top1_hit_rate": _rate("top1_hit"),
        "top3_hit_rate": _rate("top3_hit"),
        "top5_hit_rate": _rate("top5_hit"),
        "mean_elapsed_ms": _mean(elapsed),
        "p50_elapsed_ms": _percentile(elapsed, 0.50),
        "p90_elapsed_ms": _percentile(elapsed, 0.90),
        "mean_final_diagnosis_confidence_percent": _mean(confidence),
        "median_final_diagnosis_confidence_percent": round(statistics.median(confidence), 2)
        if confidence
        else 0.0,
    }


def _load_completed_case_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    completed: set[str] = set()
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except Exception:
                continue
            case_id = str(payload.get("benchmark_case_id", "")).strip()
            if case_id:
                completed.add(case_id)
    return completed


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _append_log(path: Path, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(message.rstrip() + "\n")


def run_pipeline_with_diagnostics(
    pipeline: Any,
    patient: PatientProfile,
    top_k: int,
) -> tuple[ClinicalMvpResponse, dict[str, Any]]:
    component_names = [
        "phenotype_extractor",
        "phenotype_analyser",
        "knowledge_searcher",
        "case_searcher",
        "initial_diagnosis_synthesizer",
        "disease_normalizer",
        "per_disease_verifier",
        "final_diagnosis_synthesizer",
    ]
    if not all(hasattr(pipeline, name) for name in component_names):
        response = pipeline.run(ClinicalMvpRequest(patient=patient, top_k=top_k))
        return response, {"stage_timings_ms": {}, "fallback_flags": {}}

    stage_timings_ms: dict[str, int] = {}

    def _time_stage(stage_name: str, fn):
        started = time.perf_counter()
        value = fn()
        stage_timings_ms[stage_name] = int((time.perf_counter() - started) * 1000)
        return value

    phenotypes = _time_stage("phenotype_extraction", lambda: pipeline.phenotype_extractor.extract(patient))
    phenotype_hints, phenotype_tool_runs = _time_stage(
        "phenotype_analysis",
        lambda: pipeline.phenotype_analyser.analyze_with_details(patient, phenotypes),
    )
    knowledge_evidence = _time_stage(
        "knowledge_search",
        lambda: pipeline.knowledge_searcher.search(patient, phenotypes),
    )
    similar_cases = _time_stage(
        "case_search",
        lambda: pipeline.case_searcher.search(patient, phenotypes),
    )
    initial_candidates = _time_stage(
        "initial_diagnosis",
        lambda: pipeline.initial_diagnosis_synthesizer.synthesize(
            patient,
            phenotypes,
            phenotype_hints,
            knowledge_evidence,
            similar_cases,
            top_k,
        ),
    )
    normalized_candidates = _time_stage(
        "disease_normalization",
        lambda: pipeline.disease_normalizer.normalize(initial_candidates),
    )
    reviews = _time_stage(
        "per_disease_verification",
        lambda: pipeline.per_disease_verifier.verify(
            patient,
            phenotypes,
            similar_cases,
            knowledge_evidence,
            normalized_candidates,
        ),
    )
    candidate_evidence = getattr(pipeline.per_disease_verifier, "last_candidate_evidence", []) or []
    knowledge_evidence = _extend_unique_evidence(knowledge_evidence, candidate_evidence)
    final_recommendation = _time_stage(
        "final_synthesis",
        lambda: pipeline.final_diagnosis_synthesizer.synthesize(
            patient,
            phenotypes,
            phenotype_hints,
            knowledge_evidence,
            similar_cases,
            initial_candidates,
            normalized_candidates,
            reviews,
        ),
    )
    response = ClinicalMvpResponse(
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
        stage_notes=getattr(pipeline, "stage_notes", {}).copy(),
    )

    fallback_flags = {
        "final_synthesis_fallback": response.final_recommendation.summary.startswith("Clinical MVP 已按 DeepRare 风格完成"),
        "review_llm_fallback_count": sum(
            1 for item in response.reviews if str(getattr(item, "reasoning", "")).startswith("LLM 复核兜底")
        ),
        "review_heuristic_count": sum(
            1 for item in response.reviews if str(getattr(item, "reasoning", "")).startswith("启发式复核")
        ),
        "normalized_unmapped_count": sum(
            1 for item in response.normalized_candidates if str(getattr(item, "ontology", "")).lower() == "unmapped"
        ),
        "phenotype_sources": sorted({str(getattr(item, "source", "")) for item in response.phenotypes if getattr(item, "source", "")}),
        "knowledge_evidence_count": len(response.knowledge_evidence),
        "similar_case_count": len(response.similar_cases),
    }
    return response, {
        "stage_timings_ms": stage_timings_ms,
        "fallback_flags": fallback_flags,
    }


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    tiers = {item.strip() for item in args.tiers.split(",") if item.strip()}
    output_dir = (
        Path(args.output_dir)
        if args.output_dir
        else Path("outputs") / "benchmark_runs" / f"{_timestamp_slug()}_public_cases"
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    results_jsonl = output_dir / "per_case_results.jsonl"
    responses_dir = output_dir / "responses"
    log_path = output_dir / "benchmark.log"
    tracebacks_dir = output_dir / "tracebacks"

    config = ClinicalMvpConfig.load(args.config)
    pipeline = build_clinical_mvp_pipeline(config=config)

    selected_rows = load_benchmark_rows(
        args.input,
        tiers=tiers,
        offset=max(args.offset, 0),
        limit=max(args.limit, 0),
    )
    _write_csv(output_dir / "cases_input_snapshot.csv", selected_rows)

    manifest = {
        "run_type": "fertility_public_cases_clinical_text",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "config_path": str(args.config),
        "input_path": str(args.input),
        "top_k": args.top_k,
        "offset": args.offset,
        "limit": args.limit,
        "tiers": sorted(tiers),
        "resume": bool(args.resume),
        "selected_case_count": len(selected_rows),
        "mode": "clinical-text",
    }
    _write_json(output_dir / "run_manifest.json", manifest)
    _append_log(
        log_path,
        (
            f"[{datetime.now().isoformat(timespec='seconds')}] "
            f"START total_selected={len(selected_rows)} top_k={args.top_k} "
            f"tiers={','.join(sorted(tiers))} offset={args.offset} limit={args.limit}"
        ),
    )

    completed_case_ids = _load_completed_case_ids(results_jsonl) if args.resume else set()
    case_results: list[dict[str, Any]] = []
    if args.resume and results_jsonl.exists():
        with results_jsonl.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    case_results.append(json.loads(line))

    for row in selected_rows:
        benchmark_case_id = (row.get("_id") or "").strip()
        if args.resume and benchmark_case_id in completed_case_ids:
            _append_log(log_path, f"[{datetime.now().isoformat(timespec='seconds')}] SKIP case_id={benchmark_case_id} reason=resume")
            continue

        patient = row_to_patient_profile(row)
        started = time.perf_counter()
        _append_log(
            log_path,
            (
                f"[{datetime.now().isoformat(timespec='seconds')}] RUN "
                f"case_id={benchmark_case_id} pmid={(row.get('source_pmid') or '').strip()} "
                f"gold_orpha={normalize_orpha_id(row.get('Orpha_id') or '')}"
            ),
        )
        try:
            response, diagnostics = run_pipeline_with_diagnostics(pipeline, patient, args.top_k)
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            response_payload = asdict(response)
            response_payload["benchmark_diagnostics"] = diagnostics
            _write_json(responses_dir / f"{benchmark_case_id}.json", response_payload)
            case_result = evaluate_case_result(row, response, elapsed_ms, diagnostics)
            _append_log(
                log_path,
                (
                    f"[{datetime.now().isoformat(timespec='seconds')}] DONE "
                    f"case_id={benchmark_case_id} elapsed_ms={elapsed_ms} "
                    f"top1_orpha={case_result['top1_orpha_id']} top1_hit={case_result['top1_hit']} "
                    f"phenotypes={case_result['phenotype_count']} normalized={case_result['normalized_candidate_count']} "
                    f"stages={json.dumps(diagnostics.get('stage_timings_ms', {}), ensure_ascii=False)} "
                    f"fallbacks={json.dumps(diagnostics.get('fallback_flags', {}), ensure_ascii=False)}"
                ),
            )
        except Exception as exc:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            tb_text = traceback.format_exc()
            tracebacks_dir.mkdir(parents=True, exist_ok=True)
            (tracebacks_dir / f"{benchmark_case_id}.traceback.txt").write_text(tb_text, encoding="utf-8")
            case_result = {
                "benchmark_case_id": benchmark_case_id,
                "source_dataset": (row.get("source_dataset") or "").strip(),
                "source_record_id": (row.get("source_record_id") or row.get("_id") or "").strip(),
                "source_pmid": (row.get("source_pmid") or "").strip(),
                "fertility_relevance_tier": (row.get("fertility_relevance_tier") or "").strip(),
                "gold_diagnosis_text": (row.get("diagnosis") or "").strip(),
                "gold_orpha_name": (row.get("Orpha_name") or "").strip(),
                "gold_orpha_id": normalize_orpha_id(row.get("Orpha_id") or ""),
                "status": "failed",
                "elapsed_ms": elapsed_ms,
                "top1_name": "",
                "top1_orpha_id": "",
                "predicted_orpha_ids": [],
                "predicted_diagnosis_names": [],
                "top1_hit": False,
                "top3_hit": False,
                "top5_hit": False,
                "phenotype_count": 0,
                "normalized_candidate_count": 0,
                "review_supported_count": 0,
                "final_diagnosis_confidence_percent": 0,
                "error": f"{type(exc).__name__}: {exc}",
            }
            _append_log(
                log_path,
                (
                    f"[{datetime.now().isoformat(timespec='seconds')}] FAIL "
                    f"case_id={benchmark_case_id} elapsed_ms={elapsed_ms} error={type(exc).__name__}: {exc}"
                ),
            )
        case_results.append(case_result)
        _write_jsonl(results_jsonl, case_results)

    aggregate = summarize_results(case_results)
    _write_json(output_dir / "aggregate_metrics.json", aggregate)
    _write_csv(output_dir / "per_case_summary.csv", case_results)
    failures = [item for item in case_results if item.get("status") == "failed"]
    _write_csv(output_dir / "failures.csv", failures)
    _append_log(
        log_path,
        (
            f"[{datetime.now().isoformat(timespec='seconds')}] END "
            f"completed={aggregate['completed_cases']} failed={aggregate['failed_cases']} "
            f"top1={aggregate['top1_hit_rate']} top3={aggregate['top3_hit_rate']} top5={aggregate['top5_hit_rate']}"
        ),
    )

    print(json.dumps({"output_dir": str(output_dir), **aggregate}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
