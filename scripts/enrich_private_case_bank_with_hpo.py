"""Enrich the private testing case bank with HPO terms from a clinical sheet."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import pandas as pd


HPO_PATTERN = re.compile(r"([^;；,\n\r]+?)\s*[（(]\s*(HP:\d{7})\s*[）)]")
HPO_CODE_PATTERN = re.compile(r"HP:\d{7}")


def clean_text(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return re.sub(r"\s+", " ", str(value).replace("\u00a0", " ")).strip()


def parse_hpo_cell(value: object) -> list[tuple[str, str]]:
    """Parse strings like `女性不孕症(HP:0008222);早发性卵巢功能不全(HP:0008209)`."""
    text = clean_text(value)
    if not text or text in {"/", "无", "nan"}:
        return []

    parsed: list[tuple[str, str]] = []
    for label, code in HPO_PATTERN.findall(text):
        label = clean_text(label).strip(" ;；,，、:/")
        parsed.append((code, label))

    parsed_codes = {code for code, _ in parsed}
    for code in HPO_CODE_PATTERN.findall(text):
        if code not in parsed_codes:
            parsed.append((code, ""))
    return parsed


def dedupe_terms(terms: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    seen: set[str] = set()
    output: list[tuple[str, str, str]] = []
    for role, code, label in terms:
        if code in seen:
            continue
        seen.add(code)
        output.append((role, code, label))
    return output


def build_hpo_map(hpo_path: Path, sheet_name: str) -> pd.DataFrame:
    raw = pd.read_excel(hpo_path, sheet_name=sheet_name, dtype=str)
    rows = []
    for _, row in raw.iterrows():
        project_id = clean_text(row.get("送检单编号"))
        if not project_id:
            continue
        terms: list[tuple[str, str, str]] = []
        for code, label in parse_hpo_cell(row.get("受检人HPO表型术语")):
            terms.append(("proband", code, label))
        for code, label in parse_hpo_cell(row.get("配偶HPO表型术语")):
            terms.append(("spouse", code, label))
        terms = dedupe_terms(terms)
        rows.append(
            {
                "project_id": project_id,
                "hpo_terms_new": "|".join(code for _, code, _ in terms),
                "hpo_labels": "|".join(label for _, _, label in terms if label),
                "hpo_term_details": json.dumps(
                    [
                        {"role": role, "code": code, "label": label}
                        for role, code, label in terms
                    ],
                    ensure_ascii=False,
                ),
                "proband_hpo_raw": clean_text(row.get("受检人HPO表型术语")),
                "spouse_hpo_raw": clean_text(row.get("配偶HPO表型术语")),
                "gdt_clinical_info": clean_text(row.get("临床信息")),
                "gdt_proband_clinical_info": clean_text(row.get("受检人临床表现")),
                "gdt_spouse_clinical_info": clean_text(row.get("配偶临床表现")),
                "hpo_term_count": len(terms),
            }
        )
    return pd.DataFrame(rows)


def append_hpo_to_case_report(case_report: str, hpo_terms: str, hpo_labels: str) -> str:
    case_report = clean_text(case_report).replace("\\n", "\n")
    if not hpo_terms:
        return case_report
    hpo_line = f"HPO terms: {hpo_terms}"
    if hpo_labels:
        hpo_line += f" ({hpo_labels})"
    if "HPO terms:" in case_report:
        return re.sub(r"HPO terms:\s*[^\n]*", hpo_line, case_report, count=1)
    return "\n".join(part for part in [case_report, hpo_line] if part)


def enrich(
    cases_input: Path,
    hpo_input: Path,
    cases_output: Path,
    stats_output: Path,
    sheet_name: str,
) -> None:
    cases = pd.read_csv(cases_input, dtype=str).fillna("")
    hpo_map = build_hpo_map(hpo_input, sheet_name)

    enriched = cases.merge(hpo_map, on="project_id", how="left")
    enriched["hpo_terms_new"] = enriched["hpo_terms_new"].fillna("")
    enriched["hpo_labels"] = enriched["hpo_labels"].fillna("")
    enriched["hpo_term_details"] = enriched["hpo_term_details"].fillna("[]")
    enriched["proband_hpo_raw"] = enriched["proband_hpo_raw"].fillna("")
    enriched["spouse_hpo_raw"] = enriched["spouse_hpo_raw"].fillna("")
    enriched["hpo_term_count"] = enriched["hpo_term_count"].fillna(0).astype(int)

    original_hpo = enriched["hpo_terms"].fillna("")
    enriched["hpo_terms"] = enriched["hpo_terms_new"].where(
        enriched["hpo_terms_new"].str.strip().ne(""),
        original_hpo,
    )
    enriched = enriched.drop(columns=["hpo_terms_new"])

    enriched["case_report"] = [
        append_hpo_to_case_report(row.case_report, row.hpo_terms, row.hpo_labels)
        for row in enriched.itertuples(index=False)
    ]

    cases_output.parent.mkdir(parents=True, exist_ok=True)
    enriched.to_csv(cases_output, index=False, encoding="utf-8-sig")

    matched = int(enriched["hpo_term_count"].gt(0).sum())
    stats = {
        "cases_input": str(cases_input),
        "hpo_input": str(hpo_input),
        "cases_output": str(cases_output),
        "case_rows": int(len(enriched)),
        "hpo_source_project_rows": int(len(hpo_map)),
        "matched_project_rows": int(enriched["project_id"].isin(set(hpo_map["project_id"])).sum()),
        "cases_with_parsed_hpo": matched,
        "cases_without_parsed_hpo": int(len(enriched) - matched),
        "total_parsed_hpo_terms": int(enriched["hpo_term_count"].sum()),
        "new_columns": [
            "hpo_labels",
            "hpo_term_details",
            "proband_hpo_raw",
            "spouse_hpo_raw",
            "gdt_clinical_info",
            "gdt_proband_clinical_info",
            "gdt_spouse_clinical_info",
            "hpo_term_count",
        ],
    }
    stats_output.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases-input", default="database/fertility_private_testing_cases_2025.csv")
    parser.add_argument("--hpo-input", default="database/GDT项目临床信息.xlsx")
    parser.add_argument("--sheet", default="sheet")
    parser.add_argument("--cases-output", default="database/fertility_private_testing_cases_2025.with_hpo.csv")
    parser.add_argument("--stats-output", default="database/fertility_private_testing_cases_2025.with_hpo.stats.json")
    args = parser.parse_args()
    enrich(
        Path(args.cases_input),
        Path(args.hpo_input),
        Path(args.cases_output),
        Path(args.stats_output),
        args.sheet,
    )


if __name__ == "__main__":
    main()
