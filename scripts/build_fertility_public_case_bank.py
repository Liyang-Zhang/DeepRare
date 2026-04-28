"""Build a broad fertility-related public case bank from RareArena RDS JSONL.

The output keeps DeepRare's local case-bank compatible columns:
`_id`, `case_report`, `diagnosis`, `Orpha_name`, `Orpha_id`, `age`, `gender`,
`embedding`.

It also adds fertility-specific metadata so the bank can be audited and refined
before embedding.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from dataclasses import dataclass
from json import JSONDecodeError, JSONDecoder
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class TermRule:
    term: str
    category: str
    weight: int


TERM_RULES: tuple[TermRule, ...] = (
    # Direct infertility / reproductive failure.
    TermRule("infertility", "infertility", 5),
    TermRule("infertile", "infertility", 5),
    TermRule("subfertility", "infertility", 5),
    TermRule("sterility", "infertility", 4),
    TermRule("reproductive failure", "infertility", 4),
    TermRule("failure to conceive", "infertility", 4),
    # Recurrent pregnancy loss and early embryo loss.
    TermRule("recurrent pregnancy loss", "pregnancy_loss", 5),
    TermRule("recurrent miscarriage", "pregnancy_loss", 5),
    TermRule("recurrent spontaneous abortion", "pregnancy_loss", 5),
    TermRule("spontaneous abortion", "pregnancy_loss", 4),
    TermRule("miscarriage", "pregnancy_loss", 4),
    TermRule("pregnancy loss", "pregnancy_loss", 4),
    TermRule("fetal loss", "pregnancy_loss", 3),
    TermRule("stillbirth", "pregnancy_loss", 3),
    TermRule("embryo arrest", "pregnancy_loss", 5),
    TermRule("embryonic arrest", "pregnancy_loss", 5),
    TermRule("implantation failure", "pregnancy_loss", 4),
    # Hydatidiform mole / gestational trophoblastic disease.
    TermRule("hydatidiform mole", "molar_pregnancy", 6),
    TermRule("molar pregnancy", "molar_pregnancy", 6),
    TermRule("recurrent hydatidiform", "molar_pregnancy", 7),
    TermRule("gestational trophoblastic", "molar_pregnancy", 5),
    TermRule("NLRP7", "molar_pregnancy", 6),
    TermRule("KHDC3L", "molar_pregnancy", 6),
    # Female reproductive endocrine / ovarian.
    TermRule("primary ovarian insufficiency", "ovarian_function", 6),
    TermRule("premature ovarian insufficiency", "ovarian_function", 6),
    TermRule("premature ovarian failure", "ovarian_function", 6),
    TermRule("ovarian insufficiency", "ovarian_function", 5),
    TermRule("ovarian failure", "ovarian_function", 5),
    TermRule("diminished ovarian reserve", "ovarian_function", 5),
    TermRule("anovulation", "ovarian_function", 4),
    TermRule("oligoovulation", "ovarian_function", 4),
    TermRule("amenorrhea", "ovarian_function", 3),
    TermRule("oligomenorrhea", "ovarian_function", 3),
    TermRule("polycystic ovary", "ovarian_function", 3),
    TermRule("PCOS", "ovarian_function", 3),
    TermRule("gonadal dysgenesis", "ovarian_function", 4),
    TermRule("streak gonad", "ovarian_function", 4),
    TermRule("oocyte maturation arrest", "oocyte_maturation", 7),
    TermRule("oocyte maturation defect", "oocyte_maturation", 7),
    TermRule("oocyte maturation failure", "oocyte_maturation", 7),
    TermRule("oocyte maturation", "oocyte_maturation", 5),
    TermRule("oocyte activation deficiency", "oocyte_maturation", 6),
    TermRule("oocyte activation defect", "oocyte_maturation", 6),
    TermRule("empty follicle syndrome", "oocyte_maturation", 6),
    # Male infertility / spermatogenesis.
    TermRule("azoospermia", "male_factor", 6),
    TermRule("oligospermia", "male_factor", 5),
    TermRule("oligozoospermia", "male_factor", 5),
    TermRule("asthenozoospermia", "male_factor", 5),
    TermRule("teratozoospermia", "male_factor", 5),
    TermRule("abnormal sperm morphology", "male_factor", 6),
    TermRule("sperm morphology", "male_factor", 4),
    TermRule("sperm motility", "male_factor", 4),
    TermRule("sperm abnormality", "male_factor", 4),
    TermRule("sperm abnormalities", "male_factor", 4),
    TermRule("spermatogenic failure", "male_factor", 6),
    TermRule("spermatogenesis", "male_factor", 4),
    TermRule("semen analysis", "male_factor", 4),
    TermRule("sperm count", "male_factor", 4),
    TermRule("testicular failure", "male_factor", 5),
    TermRule("hypogonadism", "male_factor", 3),
    TermRule("cryptorchidism", "male_factor", 3),
    TermRule("Klinefelter", "male_factor", 4),
    TermRule("nonobstructive azoospermia", "male_factor", 7),
    TermRule("obstructive azoospermia", "male_factor", 6),
    TermRule("cryptozoospermia", "male_factor", 5),
    TermRule("necrospermia", "male_factor", 5),
    TermRule("globozoospermia", "male_factor", 6),
    TermRule("macrozoospermia", "male_factor", 6),
    TermRule("multiple morphological abnormalities of the sperm flagella", "male_factor", 7),
    # ART / gamete / embryo terms. Lower weights because these can appear in
    # broader reproductive biology contexts.
    TermRule("in vitro fertilization", "art_gamete_embryo", 4),
    TermRule("IVF", "art_gamete_embryo", 3),
    TermRule("ICSI", "art_gamete_embryo", 3),
    TermRule("assisted reproduction", "art_gamete_embryo", 4),
    TermRule("assisted reproductive", "art_gamete_embryo", 4),
    TermRule("oocyte", "art_gamete_embryo", 3),
    TermRule("oocytes", "art_gamete_embryo", 3),
    TermRule("gamete", "art_gamete_embryo", 3),
    TermRule("fertilization", "art_gamete_embryo", 3),
    TermRule("fertilisation", "art_gamete_embryo", 3),
    # Reproductive anatomy / disease. These are intentionally moderate to weak
    # to improve recall while keeping them distinguishable from direct hits.
    TermRule("endometriosis", "reproductive_anatomy", 4),
    TermRule("fallopian tube", "reproductive_anatomy", 3),
    TermRule("hypoplasia of the fallopian tube", "reproductive_anatomy", 7),
    TermRule("fallopian tube hypoplasia", "reproductive_anatomy", 7),
    TermRule("tubal hypoplasia", "reproductive_anatomy", 6),
    TermRule("fallopian tube obstruction", "reproductive_anatomy", 5),
    TermRule("tubal obstruction", "reproductive_anatomy", 5),
    TermRule("tubal occlusion", "reproductive_anatomy", 5),
    TermRule("salpingitis", "reproductive_anatomy", 3),
    TermRule("tubal factor", "reproductive_anatomy", 4),
    TermRule("uterine anomaly", "reproductive_anatomy", 4),
    TermRule("mullerian anomaly", "reproductive_anatomy", 4),
    TermRule("müllerian anomaly", "reproductive_anatomy", 4),
    # DSD / sex development terms. These are relevant to fertility and gonadal
    # function, but can be broader than infertility itself.
    TermRule("disorders of sex development", "dsd", 6),
    TermRule("disorder of sex development", "dsd", 6),
    TermRule("DSD", "dsd", 4),
    TermRule("sex development disorder", "dsd", 6),
    TermRule("46,XX DSD", "dsd", 6),
    TermRule("46,XY DSD", "dsd", 6),
    TermRule("46 XX DSD", "dsd", 6),
    TermRule("46 XY DSD", "dsd", 6),
    TermRule("sexual development disorder", "dsd", 5),
    TermRule("sex reversal", "dsd", 5),
    TermRule("gonadal dysgenesis", "dsd", 5),
    TermRule("androgen insensitivity", "dsd", 5),
    TermRule("ovotesticular", "dsd", 5),
    TermRule("ambiguous genitalia", "dsd", 4),
    TermRule("hypospadias", "dsd", 3),
)


OUTPUT_COLUMNS = [
    "_id",
    "case_report",
    "diagnosis",
    "Orpha_name",
    "Orpha_id",
    "age",
    "gender",
    "embedding",
    "source_dataset",
    "source_record_id",
    "source_pub_date",
    "source_pmid",
    "source_title",
    "source_file_path",
    "source_url",
    "matched_terms",
    "matched_categories",
    "fertility_relevance_score",
    "fertility_relevance_tier",
    "case_text_length",
]


def normalize_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def iter_jsonl(path: Path) -> Iterable[dict]:
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)


def iter_json_array(path: Path, chunk_size: int = 1024 * 1024) -> Iterable[dict]:
    decoder = JSONDecoder()
    with path.open("r", encoding="utf-8") as handle:
        buffer = ""
        started = False
        eof = False
        while True:
            if not eof:
                chunk = handle.read(chunk_size)
                if chunk:
                    buffer += chunk
                else:
                    eof = True

            pos = 0
            length = len(buffer)
            while True:
                while pos < length and buffer[pos].isspace():
                    pos += 1
                if not started:
                    if pos >= length:
                        break
                    if buffer[pos] != "[":
                        raise ValueError(f"{path} is not a JSON array.")
                    started = True
                    pos += 1
                    continue
                while pos < length and (buffer[pos].isspace() or buffer[pos] == ","):
                    pos += 1
                if pos >= length:
                    break
                if buffer[pos] == "]":
                    return
                try:
                    item, next_pos = decoder.raw_decode(buffer, pos)
                except JSONDecodeError:
                    if eof:
                        raise
                    break
                yield item
                pos = next_pos
            buffer = buffer[pos:]
            if eof:
                break


def load_pmc_metadata(path: Path, needed_ids: set[str]) -> dict[str, dict[str, str]]:
    if not path.exists() or not needed_ids:
        return {}
    metadata: dict[str, dict[str, str]] = {}
    for record in iter_json_array(path):
        patient_uid = normalize_text(record.get("patient_uid"))
        if not patient_uid or patient_uid not in needed_ids:
            continue
        pmid = normalize_text(record.get("PMID"))
        metadata[patient_uid] = {
            "source_pmid": pmid,
            "source_title": normalize_text(record.get("title")),
            "source_file_path": normalize_text(record.get("file_path")),
            "source_url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else "",
        }
    return metadata


def match_record(record: dict) -> tuple[int, str, str, str]:
    text = " ".join(
        normalize_text(record.get(field))
        for field in ("case_report", "diagnosis", "Orpha_name")
    )
    text_lower = text.lower()
    matched: list[TermRule] = []
    for rule in TERM_RULES:
        if term_matches(text, text_lower, rule.term):
            matched.append(rule)

    if not matched:
        return 0, "", "", "none"

    score = sum(rule.weight for rule in matched)
    categories = sorted({rule.category for rule in matched})
    terms = sorted({rule.term for rule in matched}, key=str.lower)

    if score >= 8 or any(rule.weight >= 6 for rule in matched):
        tier = "strong"
    elif score >= 4:
        tier = "moderate"
    else:
        tier = "weak"
    return score, "|".join(terms), "|".join(categories), tier


def term_matches(text: str, text_lower: str, term: str) -> bool:
    """Return whether a rule term matches text.

    Very short uppercase abbreviations such as DSD/IVF/ICSI/PCOS need word-boundary
    matching. Plain substring matching incorrectly matches biomedical terms such as
    anti-dsDNA when the intended term is DSD.
    """
    if len(term) <= 4 and term.upper() == term:
        return re.search(rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])", text, re.IGNORECASE) is not None
    return term.lower() in text_lower


def build_case_row(
    record: dict,
    score: int,
    terms: str,
    categories: str,
    tier: str,
    pmc_metadata: dict[str, str] | None = None,
) -> dict:
    source_id = normalize_text(record.get("_id"))
    pub_date = normalize_text(record.get("pub_date"))
    pmc_metadata = pmc_metadata or {}
    return {
        "_id": f"rds:{source_id}",
        "case_report": normalize_text(record.get("case_report")),
        "diagnosis": normalize_text(record.get("diagnosis")),
        "Orpha_name": normalize_text(record.get("Orpha_name")),
        "Orpha_id": normalize_text(record.get("Orpha_id")),
        "age": json.dumps(record.get("age", ""), ensure_ascii=False),
        "gender": normalize_text(record.get("gender")),
        "embedding": "",
        "source_dataset": "RareArena_RDS",
        "source_record_id": source_id,
        "source_pub_date": pub_date,
        "source_pmid": pmc_metadata.get("source_pmid", ""),
        "source_title": pmc_metadata.get("source_title", ""),
        "source_file_path": pmc_metadata.get("source_file_path", ""),
        "source_url": pmc_metadata.get("source_url", ""),
        "matched_terms": terms,
        "matched_categories": categories,
        "fertility_relevance_score": str(score),
        "fertility_relevance_tier": tier,
        "case_text_length": str(len(normalize_text(record.get("case_report")))),
    }


def build(input_path: Path, pmc_v2_path: Path, output_path: Path, stats_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    stats_path.parent.mkdir(parents=True, exist_ok=True)

    records = list(iter_jsonl(input_path))
    needed_ids = {normalize_text(record.get("_id")) for record in records if normalize_text(record.get("_id"))}
    pmc_metadata = load_pmc_metadata(pmc_v2_path, needed_ids)

    total = 0
    kept = 0
    kept_with_pmc = 0
    tier_counts: Counter[str] = Counter()
    category_counts: Counter[str] = Counter()
    term_counts: Counter[str] = Counter()

    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        for record in records:
            total += 1
            score, terms, categories, tier = match_record(record)
            if score <= 0:
                continue
            row = build_case_row(
                record,
                score,
                terms,
                categories,
                tier,
                pmc_metadata.get(normalize_text(record.get("_id"))),
            )
            writer.writerow(row)
            kept += 1
            if row["source_pmid"]:
                kept_with_pmc += 1
            tier_counts[tier] += 1
            for category in categories.split("|"):
                if category:
                    category_counts[category] += 1
            for term in terms.split("|"):
                if term:
                    term_counts[term] += 1

    stats = {
        "input_path": str(input_path),
        "pmc_v2_path": str(pmc_v2_path),
        "output_path": str(output_path),
        "total_records": total,
        "kept_records": kept,
        "pmc_v2_matched_source_records": sum(
            1 for record in records if normalize_text(record.get("_id")) in pmc_metadata
        ),
        "pmc_v2_matched_kept_records": kept_with_pmc,
        "filter_recall_policy": "broad keyword match over case_report + diagnosis + Orpha_name",
        "tier_counts": dict(tier_counts),
        "category_counts": dict(category_counts),
        "top_terms": dict(term_counts.most_common(50)),
        "columns": OUTPUT_COLUMNS,
    }
    stats_path.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="database/RDS.json")
    parser.add_argument("--pmc-v2", default="database/PMC-Patients-V2.json")
    parser.add_argument("--output", default="database/fertility_public_cases_rds.csv")
    parser.add_argument("--stats", default="database/fertility_public_cases_rds.stats.json")
    args = parser.parse_args()

    build(Path(args.input), Path(args.pmc_v2), Path(args.output), Path(args.stats))


if __name__ == "__main__":
    main()
