"""Build a private historical testing case bank from the 2025 WES spreadsheet.

The source sheet is variant-level: one row per variant, with project-level
fields often only filled on the first row of the same project. This script
creates:

1. A case-level table, one row per project, compatible with DeepRare-style
   local case retrieval.
2. A variant-level cleaned table for audit and later structured reasoning.

Names and hospital sample codes are intentionally excluded from outputs.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

import pandas as pd


SOURCE_COLUMNS = {
    "project_id": "送检单编号",
    "patient_name": "受检人姓名",
    "sex": "性别",
    "age": "年龄",
    "spouse_name": "配偶姓名",
    "spouse_sex": "配偶性别",
    "spouse_age": "配偶年龄",
    "test_project": "检测项目",
    "source_org": "送检单位",
    "sample_type": "样本类型",
    "clinical_info": "受检人临床表现",
    "spouse_clinical_info": "配偶临床表现",
    "proband_conclusion": "受检人检测结论",
    "spouse_conclusion": "配偶检测结论",
    "report_status": "报告状态",
    "result_note": "检测结果说明",
    "proband_advice": "受检人咨询建议",
    "spouse_advice": "配偶咨询建议",
    "fmr1_result": "女性FMR1基因CGG重复数+检测结果",
    "y_microdeletion_result": "Y微缺-结果解读",
    "report_date": "报告日期",
}

VARIANT_COLUMNS = {
    "list_category": "列表分类",
    "gene": "基因",
    "chromosome_position": "染色体位置",
    "variant": "变异信息",
    "gene_region": "基因亚区",
    "population_frequency": "人群频率",
    "zygosity": "合子类型",
    "variant_rating": "变异评级",
    "related_disease": "相关疾病",
    "variant_origin": "变异来源",
    "rating_evidence": "评级依据",
    "variant_interpretation": "变异解读",
    "disease_explanation": "疾病解释",
    "literature_note": "文献记录备注",
    "references": "参考文献",
    "cnv_list_category": "cnv列表分类",
    "cnv_variant": "cnv变异信息",
    "mosaic_ratio": "嵌合比例",
    "cnv_type": "cnv变异类型",
    "cnv_size": "片段大小",
    "cnv_rating": "cnv变异评级",
}

CASE_COLUMNS = [
    # DeepRare-compatible fields.
    "_id",
    "case_report",
    "diagnosis",
    "Orpha_name",
    "Orpha_id",
    "age",
    "gender",
    "embedding",
    # Private testing case extensions.
    "project_id",
    "case_kind",
    "diagnosis_status",
    "clinical_suspected_diagnosis",
    "hpo_terms",
    "clinical_info",
    "spouse_clinical_info",
    "test_project",
    "sample_type",
    "source_org",
    "spouse_age",
    "spouse_gender",
    "report_status",
    "report_date",
    "reported_genes",
    "phenotype_relevant_genes",
    "variant_summary",
    "variant_interpretation_summary",
    "proband_conclusion",
    "spouse_conclusion",
    "fmr1_result",
    "y_microdeletion_result",
    "variant_count",
    "phenotype_relevant_variant_count",
    "carrier_variant_count",
    "cnv_count",
    "retrieval_tags",
    "data_quality",
]


PHENOTYPE_RELEVANT_CATEGORIES = {
    "表型高度相关",
    "表1_表型高度相关",
    "表型相关性较高",
    "表型潜在相关",
    "表2_表型潜在相关",
}

CARRIER_CATEGORIES = {"携带者筛查", "附录_携筛相关罕见变异"}


def clean_value(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    text = str(value).replace("\u00a0", " ")
    return re.sub(r"\s+", " ", text).strip()


def first_non_empty(series: pd.Series) -> str:
    for value in series:
        text = clean_value(value)
        if text:
            return text
    return ""


def redact_known_names(text: str, names: list[str]) -> str:
    redacted = clean_value(text)
    for name in names:
        name = clean_value(name)
        if len(name) >= 2:
            redacted = redacted.replace(name, "<NAME>")
    return redacted


def join_unique(values: list[str], sep: str = "|") -> str:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        text = clean_value(value)
        if text and text not in seen:
            seen.add(text)
            output.append(text)
    return sep.join(output)


def normalize_report_status(value: str, proband_conclusion: str, spouse_conclusion: str) -> str:
    combined = " ".join([value, proband_conclusion, spouse_conclusion])
    if "阳性" in combined:
        return "positive"
    if "阴性" in combined or "未检出" in combined:
        return "negative"
    return "unknown"


def infer_tags(clinical_text: str, variants: pd.DataFrame) -> str:
    text = clinical_text.lower()
    tags: list[str] = []
    rules = [
        ("recurrent_pregnancy_loss", ["复发性流产", "反复流产", "胚胎停育", "recurrent pregnancy loss", "miscarriage"]),
        ("infertility", ["不孕", "不育", "infertility"]),
        ("molar_pregnancy", ["葡萄胎", "hydatidiform", "molar pregnancy"]),
        ("poi", ["卵巢早衰", "卵巢功能不全", "poi", "premature ovarian"]),
        ("male_factor", ["无精", "少精", "弱精", "畸精", "azoospermia", "oligozoospermia"]),
        ("dsd", ["性发育", "dsd", "外生殖器", "隐睾", "尿道下裂"]),
        ("carrier_screening", ["携带者筛查"]),
    ]
    for tag, keywords in rules:
        if any(keyword in text for keyword in keywords):
            tags.append(tag)

    categories = {clean_value(value) for value in variants.get("list_category", [])}
    if categories & PHENOTYPE_RELEVANT_CATEGORIES:
        tags.append("phenotype_relevant_variant")
    if categories & CARRIER_CATEGORIES:
        tags.append("carrier_variant")
    if variants.get("cnv_variant", pd.Series(dtype=str)).fillna("").astype(str).str.strip().any():
        tags.append("cnv")
    return join_unique(tags)


def variant_label(row: pd.Series) -> str:
    gene = clean_value(row.get("gene"))
    variant = clean_value(row.get("variant"))
    rating = clean_value(row.get("variant_rating"))
    origin = clean_value(row.get("variant_origin"))
    disease = clean_value(row.get("related_disease"))
    cnv = clean_value(row.get("cnv_variant"))
    cnv_rating = clean_value(row.get("cnv_rating"))
    if gene or variant:
        parts = [gene, variant, rating, origin, disease]
        return "; ".join(part for part in parts if part)
    if cnv:
        parts = [cnv, clean_value(row.get("cnv_type")), clean_value(row.get("cnv_size")), cnv_rating]
        return "; ".join(part for part in parts if part)
    return ""


def build_case_report(project: dict[str, str], variants: pd.DataFrame) -> str:
    phenotype_variants = variants[variants["list_category"].isin(PHENOTYPE_RELEVANT_CATEGORIES)]
    if phenotype_variants.empty:
        phenotype_variants = variants.head(8)

    variant_lines = [variant_label(row) for _, row in phenotype_variants.head(12).iterrows()]
    variant_lines = [line for line in variant_lines if line]

    sections = [
        f"Clinical information: {project['clinical_info']}" if project["clinical_info"] else "",
        f"Spouse clinical information: {project['spouse_clinical_info']}" if project["spouse_clinical_info"] else "",
        "HPO terms: ",
        f"Testing project: {project['test_project']}" if project["test_project"] else "",
        f"Testing conclusion: {project['proband_conclusion']}" if project["proband_conclusion"] else "",
        f"Spouse testing conclusion: {project['spouse_conclusion']}" if project["spouse_conclusion"] else "",
        f"Genetic findings: {' | '.join(variant_lines)}" if variant_lines else "",
        f"FMR1 result: {project['fmr1_result']}" if project["fmr1_result"] else "",
        f"Y microdeletion result: {project['y_microdeletion_result']}" if project["y_microdeletion_result"] else "",
    ]
    return "\n".join(section for section in sections if section)


def build_case_label(project: dict[str, str], variants: pd.DataFrame) -> tuple[str, str]:
    phenotype_variants = variants[variants["list_category"].isin(PHENOTYPE_RELEVANT_CATEGORIES)]
    genes = join_unique(phenotype_variants["gene"].tolist()) if not phenotype_variants.empty else ""
    clinical = project["clinical_info"] or project["spouse_clinical_info"]
    if genes:
        return f"No final diagnosis; phenotype-matched variants in {genes}", genes
    all_genes = join_unique(variants["gene"].dropna().astype(str).head(8).tolist())
    if all_genes:
        return f"No final diagnosis; testing case with reported genes: {all_genes}", ""
    if clinical:
        return "No final diagnosis; clinical testing case", ""
    return "No final diagnosis; historical testing case", ""


def data_quality(project: dict[str, str], variants: pd.DataFrame) -> str:
    score = 0
    if project["clinical_info"] or project["spouse_clinical_info"]:
        score += 1
    if not variants.empty:
        score += 1
    if project["proband_conclusion"] or project["spouse_conclusion"]:
        score += 1
    if project["report_status"]:
        score += 1
    return {0: "very_low", 1: "low", 2: "medium", 3: "high", 4: "high"}[score]


def build(input_path: Path, sheet_name: str, cases_output: Path, variants_output: Path, stats_output: Path) -> None:
    df = pd.read_excel(input_path, sheet_name=sheet_name)
    df = df.rename(columns={v: k for k, v in {**SOURCE_COLUMNS, **VARIANT_COLUMNS}.items()})
    df = df[df["project_id"].notna()].copy()
    for column in set(SOURCE_COLUMNS) | set(VARIANT_COLUMNS):
        if column not in df.columns:
            df[column] = ""

    variants = df[["project_id", *VARIANT_COLUMNS.keys()]].copy()
    for column in variants.columns:
        variants[column] = variants[column].map(clean_value)
    variants = variants[
        variants["gene"].ne("")
        | variants["variant"].ne("")
        | variants["cnv_variant"].ne("")
        | variants["cnv_type"].ne("")
    ].copy()
    variants.insert(0, "variant_record_id", [f"var_{idx + 1:07d}" for idx in range(len(variants))])
    variants.to_csv(variants_output, index=False, encoding="utf-8-sig")

    case_rows: list[dict[str, str]] = []
    for project_id, group in df.groupby("project_id", sort=False):
        project = {name: first_non_empty(group[name]) for name in SOURCE_COLUMNS if name in group.columns}
        project["project_id"] = clean_value(project_id)
        names_to_redact = [project.get("patient_name", ""), project.get("spouse_name", "")]
        for text_field in (
            "clinical_info",
            "spouse_clinical_info",
            "proband_conclusion",
            "spouse_conclusion",
            "result_note",
            "proband_advice",
            "spouse_advice",
            "fmr1_result",
            "y_microdeletion_result",
        ):
            project[text_field] = redact_known_names(project.get(text_field, ""), names_to_redact)
        project_variants = variants[variants["project_id"] == project["project_id"]].copy()
        label, phenotype_genes = build_case_label(project, project_variants)
        all_genes = join_unique(project_variants["gene"].tolist())
        phenotype_count = int(project_variants["list_category"].isin(PHENOTYPE_RELEVANT_CATEGORIES).sum())
        carrier_count = int(project_variants["list_category"].isin(CARRIER_CATEGORIES).sum())
        cnv_count = int(project_variants["cnv_variant"].ne("").sum())
        clinical_text = " ".join([project["clinical_info"], project["spouse_clinical_info"]])
        row = {
            "_id": f"private:{project['project_id']}",
            "case_report": build_case_report(project, project_variants),
            "diagnosis": label,
            "Orpha_name": "",
            "Orpha_id": "",
            "age": project["age"],
            "gender": project["sex"],
            "embedding": "",
            "project_id": project["project_id"],
            "case_kind": "private_historical_testing_case",
            "diagnosis_status": "no_final_diagnosis",
            "clinical_suspected_diagnosis": "",
            "hpo_terms": "",
            "clinical_info": project["clinical_info"],
            "spouse_clinical_info": project["spouse_clinical_info"],
            "test_project": project["test_project"],
            "sample_type": project["sample_type"],
            "source_org": project["source_org"],
            "spouse_age": project["spouse_age"],
            "spouse_gender": project["spouse_sex"],
            "report_status": normalize_report_status(project["report_status"], project["proband_conclusion"], project["spouse_conclusion"]),
            "report_date": project["report_date"],
            "reported_genes": all_genes,
            "phenotype_relevant_genes": phenotype_genes,
            "variant_summary": " | ".join([variant_label(row) for _, row in project_variants.head(30).iterrows() if variant_label(row)]),
            "variant_interpretation_summary": join_unique(project_variants["variant_rating"].tolist()),
            "proband_conclusion": project["proband_conclusion"],
            "spouse_conclusion": project["spouse_conclusion"],
            "fmr1_result": project["fmr1_result"],
            "y_microdeletion_result": project["y_microdeletion_result"],
            "variant_count": str(len(project_variants)),
            "phenotype_relevant_variant_count": str(phenotype_count),
            "carrier_variant_count": str(carrier_count),
            "cnv_count": str(cnv_count),
            "retrieval_tags": infer_tags(clinical_text, project_variants),
            "data_quality": data_quality(project, project_variants),
        }
        case_rows.append(row)

    cases = pd.DataFrame(case_rows, columns=CASE_COLUMNS)
    cases.to_csv(cases_output, index=False, encoding="utf-8-sig")

    stats = {
        "input_path": str(input_path),
        "sheet_name": sheet_name,
        "case_output": str(cases_output),
        "variant_output": str(variants_output),
        "source_rows": int(len(df)),
        "case_rows": int(len(cases)),
        "variant_rows": int(len(variants)),
        "report_status_counts": cases["report_status"].value_counts(dropna=False).to_dict(),
        "data_quality_counts": cases["data_quality"].value_counts(dropna=False).to_dict(),
        "test_project_counts": cases["test_project"].value_counts(dropna=False).head(30).to_dict(),
        "retrieval_tag_counts": Counter(
            tag
            for tags in cases["retrieval_tags"].fillna("")
            for tag in tags.split("|")
            if tag
        ),
        "columns": {
            "cases": CASE_COLUMNS,
            "variants": list(variants.columns),
        },
        "privacy_note": (
            "Name columns and hospital sample codes are excluded. Exact patient/spouse names "
            "from source columns are redacted in free text. Other names embedded in clinical "
            "notes may require additional de-identification review. Project IDs are retained "
            "for internal traceability."
        ),
    }
    stats["retrieval_tag_counts"] = dict(stats["retrieval_tag_counts"])
    stats_output.write_text(json.dumps(stats, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="database/WES类项目数据统计25年截止12月.xlsx")
    parser.add_argument("--sheet", default="合并")
    parser.add_argument("--cases-output", default="database/fertility_private_testing_cases_2025.csv")
    parser.add_argument("--variants-output", default="database/fertility_private_testing_variants_2025.csv")
    parser.add_argument("--stats-output", default="database/fertility_private_testing_cases_2025.stats.json")
    args = parser.parse_args()
    build(
        Path(args.input),
        args.sheet,
        Path(args.cases_output),
        Path(args.variants_output),
        Path(args.stats_output),
    )


if __name__ == "__main__":
    main()
