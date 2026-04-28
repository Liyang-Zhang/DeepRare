"""Command-line entrypoints for yk-FERTA development workflows."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from yk_ferta.agents.factory import build_clinical_mvp_pipeline
from yk_ferta.config import ClinicalMvpConfig
from yk_ferta.schemas.clinical import PatientProfile, PhenotypeItem
from yk_ferta.schemas.mvp import ClinicalMvpRequest, ClinicalMvpResponse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="yk-ferta-clinical-mvp",
        description="Run the yk-FERTA clinical MVP in full-note or phenotype-first mode.",
    )
    parser.add_argument(
        "--config",
        default="config/clinical_mvp.json",
        help="Path to the clinical MVP JSON config.",
    )
    parser.add_argument(
        "--input-json",
        help="Optional JSON file containing PatientProfile-compatible fields.",
    )
    parser.add_argument("--patient-id", default=None, help="Patient identifier.")
    parser.add_argument("--chief-complaint", default=None, help="Chief complaint.")
    parser.add_argument("--present-illness", default=None, help="Present illness.")
    parser.add_argument("--history", default=None, help="Relevant medical history.")
    parser.add_argument("--physical-exam", default=None, help="Physical exam findings.")
    parser.add_argument(
        "--laboratory-findings",
        default=None,
        help="Laboratory findings.",
    )
    parser.add_argument("--imaging-findings", default=None, help="Imaging findings.")
    parser.add_argument("--treatments", default=None, help="Treatments or interventions.")
    parser.add_argument("--raw-note", default=None, help="Raw clinical note text.")
    parser.add_argument(
        "--note-file",
        help="Optional text file for the raw clinical note. Appended to --raw-note when both are set.",
    )
    parser.add_argument(
        "--phenotype",
        action="append",
        default=[],
        help="Manual phenotype label. Repeat this argument to provide multiple phenotypes.",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=5,
        help="Maximum number of initial candidates to keep.",
    )
    parser.add_argument(
        "--output-format",
        choices=("text", "json"),
        default="text",
        help="Render a human-readable summary or machine-readable JSON.",
    )
    parser.add_argument(
        "--save-output",
        help="Optional path to save the rendered output. Uses the selected --output-format.",
    )
    return parser


def _patient_from_args(args: argparse.Namespace) -> PatientProfile:
    payload: dict[str, Any] = {}
    if args.input_json:
        payload = json.loads(Path(args.input_json).read_text(encoding="utf-8"))

    raw_note = payload.get("raw_note", "") or ""
    if args.note_file:
        note_text = Path(args.note_file).read_text(encoding="utf-8").strip()
        raw_note = "\n".join(part for part in [raw_note, note_text] if part)

    if args.raw_note:
        raw_note = "\n".join(part for part in [raw_note, args.raw_note.strip()] if part)

    return PatientProfile(
        patient_id=args.patient_id or payload.get("patient_id", "dev-case-001"),
        chief_complaint=args.chief_complaint or payload.get("chief_complaint", ""),
        present_illness=args.present_illness or payload.get("present_illness", ""),
        history=args.history or payload.get("history", ""),
        physical_exam=args.physical_exam or payload.get("physical_exam", ""),
        laboratory_findings=args.laboratory_findings or payload.get("laboratory_findings", ""),
        imaging_findings=args.imaging_findings or payload.get("imaging_findings", ""),
        treatments=args.treatments or payload.get("treatments", ""),
        raw_note=raw_note,
        metadata=payload.get("metadata", {}),
    )


def _manual_phenotypes_from_args(args: argparse.Namespace) -> list[PhenotypeItem]:
    return [
        PhenotypeItem(label=item.strip(), source="cli-manual", confidence=1.0)
        for item in args.phenotype
        if item and item.strip()
    ]


def run_pipeline(
    response_pipeline,
    patient: PatientProfile,
    top_k: int,
    manual_phenotypes: list[PhenotypeItem] | None = None,
) -> ClinicalMvpResponse:
    if not manual_phenotypes:
        return response_pipeline.run(ClinicalMvpRequest(patient=patient, top_k=top_k))

    phenotypes = manual_phenotypes
    phenotype_hints, phenotype_tool_runs = response_pipeline.phenotype_analyser.analyze_with_details(
        patient, phenotypes
    )
    knowledge_evidence = response_pipeline.knowledge_searcher.search(patient, phenotypes)
    similar_cases = response_pipeline.case_searcher.search(patient, phenotypes)
    initial_candidates = response_pipeline.initial_diagnosis_synthesizer.synthesize(
        patient,
        phenotypes,
        phenotype_hints,
        knowledge_evidence,
        similar_cases,
        top_k,
    )
    normalized_candidates = response_pipeline.disease_normalizer.normalize(initial_candidates)
    reviews = response_pipeline.per_disease_verifier.verify(
        patient,
        phenotypes,
        similar_cases,
        knowledge_evidence,
        normalized_candidates,
    )
    final_recommendation = response_pipeline.final_diagnosis_synthesizer.synthesize(
        patient,
        phenotypes,
        phenotype_hints,
        knowledge_evidence,
        similar_cases,
        initial_candidates,
        normalized_candidates,
        reviews,
    )
    stage_notes = response_pipeline.stage_notes.copy()
    stage_notes["entry_mode"] = "manual-phenotypes"
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
        stage_notes=stage_notes,
    )


def response_to_dict(response: ClinicalMvpResponse) -> dict[str, Any]:
    return asdict(response)


def render_text(response: ClinicalMvpResponse) -> str:
    lines: list[str] = []
    lines.append(f"Patient: {response.patient_id}")
    lines.append(f"Stage mode: {response.stage_notes.get('entry_mode', 'full-note')}")
    lines.append("")
    lines.append("Phenotypes")
    for item in response.phenotypes[:10]:
        code = f" [{item.code}]" if item.code else ""
        lines.append(f"- {item.label}{code} ({item.source})")
    lines.append("")
    lines.append("Phenotype Hints")
    for item in response.phenotype_hints[:10]:
        lines.append(
            f"- {item.source}: {item.disease_name}"
            f" | id={item.disease_id or 'n/a'} | score={item.score}"
        )
    lines.append("")
    lines.append("Phenotype Tool Runs")
    for item in response.phenotype_tool_runs[:10]:
        detail = f" | error={item.error}" if item.error else ""
        lines.append(
            f"- {item.source}: {item.status}"
            f" | candidates={len(item.parsed_candidates)}"
            f" | elapsed_ms={item.elapsed_ms}{detail}"
        )
    lines.append("")
    lines.append("Evidence")
    for item in response.knowledge_evidence[:10]:
        lines.append(
            f"- {item.source_type}: {item.title}"
            f" | source_id={item.source_id}"
            f" | summary={item.summary}"
        )
    lines.append("")
    lines.append("Similar Cases")
    for item in response.similar_cases[:10]:
        lines.append(
            f"- {item.source} [{item.evidence_role}]: {item.diagnosis}"
            f" | score={item.score}"
            f" | case_id={item.case_id}"
            f" | genes={','.join(item.phenotype_relevant_genes or item.reported_genes[:5])}"
            f" | summary={item.summary}"
        )
    lines.append("")
    lines.append("Initial Candidates")
    for item in response.initial_candidates[:10]:
        lines.append(f"- #{item.rank} {item.name} | score={item.score} | {item.rationale}")
    lines.append("")
    lines.append("Normalized Candidates")
    for item in response.normalized_candidates[:10]:
        lines.append(
            f"- {item.original_name} -> {item.normalized_name}"
            f" | id={item.disease_id or 'n/a'} | ontology={item.ontology}"
        )
    lines.append("")
    lines.append("Reviews")
    for item in response.reviews[:10]:
        lines.append(
            f"- {item.candidate_name} | supported={item.is_supported}"
            f" | confidence={item.confidence} | {item.reasoning}"
        )
    lines.append("")
    lines.append("Final Summary")
    lines.append(response.final_recommendation.summary)
    lines.append("")
    lines.append("Next Steps")
    for item in response.final_recommendation.next_steps[:10]:
        lines.append(f"- {item}")
    lines.append("")
    lines.append("Cautions")
    for item in response.final_recommendation.cautions[:10]:
        lines.append(f"- {item}")
    return "\n".join(lines)


def render_output(response: ClinicalMvpResponse, output_format: str) -> str:
    if output_format == "json":
        return json.dumps(response_to_dict(response), ensure_ascii=False, indent=2)
    return render_text(response)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    config = ClinicalMvpConfig.load(args.config)
    pipeline = build_clinical_mvp_pipeline(config=config)
    patient = _patient_from_args(args)
    manual_phenotypes = _manual_phenotypes_from_args(args)

    response = run_pipeline(
        pipeline,
        patient=patient,
        top_k=args.top_k,
        manual_phenotypes=manual_phenotypes or None,
    )

    rendered = render_output(response, args.output_format)
    if args.save_output:
        output_path = Path(args.save_output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered + ("\n" if not rendered.endswith("\n") else ""), encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
