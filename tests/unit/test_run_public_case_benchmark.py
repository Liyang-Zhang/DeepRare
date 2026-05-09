import csv
import json
from dataclasses import dataclass, field

from scripts.run_public_case_benchmark import (
    load_benchmark_rows,
    main,
    normalize_orpha_id,
    row_to_patient_profile,
    summarize_results,
)


@dataclass
class _FakePhenotype:
    label: str
    chinese_label: str = ""
    code: str | None = None
    source: str = ""
    confidence: float | None = None
    notes: str = ""


@dataclass
class _FakeNormalizedDisease:
    original_name: str
    normalized_name: str
    disease_id: str | None = None
    ontology: str = ""
    mapping_score: float | None = None
    normalization_top_matches: list[dict[str, object]] = field(default_factory=list)
    normalization_decision_source: str = ""
    normalization_decision_reason: str = ""
    normalization_decision_confidence: float | None = None


@dataclass
class _FakeReview:
    candidate_name: str
    is_supported: bool
    confidence: float | None = None
    reasoning: str = ""
    evidence_ids: list[str] = field(default_factory=list)
    supporting_evidence: list[str] = field(default_factory=list)
    contradicting_evidence: list[str] = field(default_factory=list)
    missing_evidence: list[str] = field(default_factory=list)


@dataclass
class _FakeRecommendation:
    summary: str
    candidates: list[object]
    evidence: list[object]
    reviews: list[object]
    next_steps: list[str] = field(default_factory=list)
    cautions: list[str] = field(default_factory=list)
    final_diagnosis_confidence: float = 0.0
    final_diagnosis_confidence_percent: int = 0
    diagnosis_cards: list[dict[str, object]] = field(default_factory=list)


@dataclass
class _FakeResponse:
    patient_id: str
    phenotypes: list[object]
    phenotype_hints: list[object]
    phenotype_tool_runs: list[object]
    knowledge_evidence: list[object]
    similar_cases: list[object]
    initial_candidates: list[object]
    normalized_candidates: list[object]
    reviews: list[object]
    final_recommendation: object
    stage_notes: dict[str, str] = field(default_factory=dict)


class _FakePipeline:
    def run(self, request):
        return _FakeResponse(
            patient_id=request.patient.patient_id,
            phenotypes=[_FakePhenotype(label="Infertility", code="HP:0000789")],
            phenotype_hints=[],
            phenotype_tool_runs=[],
            knowledge_evidence=[],
            similar_cases=[],
            initial_candidates=[],
            normalized_candidates=[
                _FakeNormalizedDisease(
                    original_name="Hydatidiform mole",
                    normalized_name="Hydatidiform mole",
                    disease_id="ORPHA:99927",
                    ontology="Orphanet",
                )
            ],
            reviews=[_FakeReview(candidate_name="Hydatidiform mole", is_supported=True, confidence=0.91)],
            final_recommendation=_FakeRecommendation(
                summary="summary",
                candidates=[],
                evidence=[],
                reviews=[],
                final_diagnosis_confidence=0.91,
                final_diagnosis_confidence_percent=91,
                diagnosis_cards=[
                    {
                        "clinical_diagnosis": "复发性葡萄胎",
                        "orphanet_id": "ORPHA:99927",
                    }
                ],
            ),
        )


def test_load_benchmark_rows_filters_missing_gold_fields(tmp_path):
    input_path = tmp_path / "public.csv"
    with input_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "_id",
                "case_report",
                "diagnosis",
                "Orpha_name",
                "Orpha_id",
                "fertility_relevance_tier",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "_id": "ok-1",
                "case_report": "case",
                "diagnosis": "Hydatidiform mole",
                "Orpha_name": "Hydatidiform mole",
                "Orpha_id": "99927",
                "fertility_relevance_tier": "strong",
            }
        )
        writer.writerow(
            {
                "_id": "bad-1",
                "case_report": "case",
                "diagnosis": "",
                "Orpha_name": "Hydatidiform mole",
                "Orpha_id": "99927",
                "fertility_relevance_tier": "strong",
            }
        )

    rows = load_benchmark_rows(input_path, tiers={"strong"})
    assert len(rows) == 1
    assert rows[0]["_id"] == "ok-1"


def test_row_to_patient_profile_uses_case_report_and_gold_metadata():
    patient = row_to_patient_profile(
        {
            "_id": "rds:1",
            "case_report": "A woman with infertility.",
            "diagnosis": "Hydatidiform mole",
            "Orpha_name": "Hydatidiform mole",
            "Orpha_id": "99927",
            "source_dataset": "RareArena_RDS",
            "source_pmid": "12345678",
        }
    )
    assert patient.patient_id == "rds:1"
    assert patient.raw_note == "A woman with infertility."
    assert patient.metadata["gold_orpha_id"] == "ORPHA:99927"


def test_summarize_results_reports_hit_rates():
    summary = summarize_results(
        [
            {"status": "completed", "elapsed_ms": 100, "top1_hit": True, "top3_hit": True, "top5_hit": True, "final_diagnosis_confidence_percent": 90},
            {"status": "completed", "elapsed_ms": 300, "top1_hit": False, "top3_hit": True, "top5_hit": True, "final_diagnosis_confidence_percent": 80},
            {"status": "failed", "elapsed_ms": 50, "top1_hit": False, "top3_hit": False, "top5_hit": False, "final_diagnosis_confidence_percent": 0},
        ]
    )
    assert summary["total_cases"] == 3
    assert summary["completed_cases"] == 2
    assert summary["top1_hit_rate"] == 0.5
    assert summary["top3_hit_rate"] == 1.0
    assert summary["workflow_success_rate"] == 0.6667


def test_main_runs_serial_benchmark_and_writes_outputs(tmp_path, monkeypatch, capsys):
    input_path = tmp_path / "public.csv"
    with input_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "_id",
                "case_report",
                "diagnosis",
                "Orpha_name",
                "Orpha_id",
                "source_dataset",
                "source_record_id",
                "source_pmid",
                "fertility_relevance_tier",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "_id": "rds:1",
                "case_report": "A woman with recurrent hydatidiform mole and infertility.",
                "diagnosis": "Hydatidiform mole",
                "Orpha_name": "Hydatidiform mole",
                "Orpha_id": "99927",
                "source_dataset": "RareArena_RDS",
                "source_record_id": "1",
                "source_pmid": "12345678",
                "fertility_relevance_tier": "strong",
            }
        )

    class _FakeConfig:
        pass

    monkeypatch.setattr("scripts.run_public_case_benchmark.ClinicalMvpConfig.load", lambda path: _FakeConfig())
    monkeypatch.setattr("scripts.run_public_case_benchmark.build_clinical_mvp_pipeline", lambda config: _FakePipeline())

    output_dir = tmp_path / "outputs"
    exit_code = main(
        [
            "--config",
            str(tmp_path / "clinical_mvp.json"),
            "--input",
            str(input_path),
            "--output-dir",
            str(output_dir),
        ]
    )

    printed = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert printed["completed_cases"] == 1
    assert printed["top1_hit_rate"] == 1.0
    assert (output_dir / "run_manifest.json").exists()
    assert (output_dir / "aggregate_metrics.json").exists()
    assert (output_dir / "per_case_summary.csv").exists()
    assert (output_dir / "responses" / "rds:1.json").exists()
    result_lines = (output_dir / "per_case_results.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(result_lines) == 1
    payload = json.loads(result_lines[0])
    assert payload["benchmark_case_id"] == "rds:1"
    assert payload["top1_orpha_id"] == "ORPHA:99927"
    assert normalize_orpha_id("99927") == "ORPHA:99927"
    assert (output_dir / "benchmark.log").exists()
    assert "DONE case_id=rds:1" in (output_dir / "benchmark.log").read_text(encoding="utf-8")


def test_main_writes_traceback_on_failure(tmp_path, monkeypatch, capsys):
    input_path = tmp_path / "public.csv"
    with input_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "_id",
                "case_report",
                "diagnosis",
                "Orpha_name",
                "Orpha_id",
                "source_dataset",
                "source_record_id",
                "source_pmid",
                "fertility_relevance_tier",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "_id": "rds:fail",
                "case_report": "A failing case.",
                "diagnosis": "Hydatidiform mole",
                "Orpha_name": "Hydatidiform mole",
                "Orpha_id": "99927",
                "source_dataset": "RareArena_RDS",
                "source_record_id": "fail",
                "source_pmid": "00000000",
                "fertility_relevance_tier": "strong",
            }
        )

    class _FakeConfig:
        pass

    class _FailingPipeline:
        def run(self, request):
            raise RuntimeError("boom")

    monkeypatch.setattr("scripts.run_public_case_benchmark.ClinicalMvpConfig.load", lambda path: _FakeConfig())
    monkeypatch.setattr("scripts.run_public_case_benchmark.build_clinical_mvp_pipeline", lambda config: _FailingPipeline())

    output_dir = tmp_path / "outputs"
    exit_code = main(
        [
            "--config",
            str(tmp_path / "clinical_mvp.json"),
            "--input",
            str(input_path),
            "--output-dir",
            str(output_dir),
        ]
    )

    capsys.readouterr()
    assert exit_code == 0
    tb_path = output_dir / "tracebacks" / "rds:fail.traceback.txt"
    assert tb_path.exists()
    assert "RuntimeError: boom" in tb_path.read_text(encoding="utf-8")
