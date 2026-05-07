"""Local HPO/CHPO lookup utilities for phenotype review."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


@dataclass(frozen=True)
class HpoCatalogEntry:
    code: str
    label: str
    chinese_label: str = ""
    source: str = "phenotype_mapping"


def _norm(text: str) -> str:
    return " ".join(str(text).lower().strip().split())


@lru_cache(maxsize=1)
def load_hpo_catalog(
    chpo_path: str = "database/CHPO-2025-4.xlsx",
    phenotype_mapping_path: str = "database/phenotype_mapping.json",
) -> list[HpoCatalogEntry]:
    entries: dict[str, HpoCatalogEntry] = {}

    mapping_file = Path(phenotype_mapping_path)
    if mapping_file.exists():
        data = json.loads(mapping_file.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            for code, label in data.items():
                if str(code).startswith("HP:"):
                    entries[str(code)] = HpoCatalogEntry(code=str(code), label=str(label), source="phenotype_mapping")

    chpo_file = Path(chpo_path)
    if chpo_file.exists():
        import pandas as pd

        frame = pd.read_excel(chpo_file)
        for _, row in frame.iterrows():
            code = str(row.get("HPO编号", "")).strip()
            if not code.startswith("HP:"):
                continue
            label = str(row.get("英 文", "")).strip()
            chinese = str(row.get("中文翻译", "")).strip()
            if not label and code in entries:
                label = entries[code].label
            if not label:
                continue
            entries[code] = HpoCatalogEntry(
                code=code,
                label=label,
                chinese_label=chinese if chinese != "nan" else "",
                source="CHPO-2025-4",
            )

    return list(entries.values())


@lru_cache(maxsize=1)
def _hpo_catalog_index(
    chpo_path: str = "database/CHPO-2025-4.xlsx",
    phenotype_mapping_path: str = "database/phenotype_mapping.json",
) -> tuple[dict[str, HpoCatalogEntry], dict[str, HpoCatalogEntry]]:
    code_index: dict[str, HpoCatalogEntry] = {}
    label_index: dict[str, HpoCatalogEntry] = {}
    for entry in load_hpo_catalog(chpo_path=chpo_path, phenotype_mapping_path=phenotype_mapping_path):
        code_index[entry.code] = entry
        for key in {entry.label, entry.chinese_label}:
            normalized = _norm(key)
            if normalized and normalized not in label_index:
                label_index[normalized] = entry
    return code_index, label_index


def lookup_hpo_catalog(
    code: str | None = None,
    label: str | None = None,
    *,
    chpo_path: str = "database/CHPO-2025-4.xlsx",
    phenotype_mapping_path: str = "database/phenotype_mapping.json",
) -> HpoCatalogEntry | None:
    code_index, label_index = _hpo_catalog_index(
        chpo_path=chpo_path,
        phenotype_mapping_path=phenotype_mapping_path,
    )
    if code:
        matched = code_index.get(str(code).strip())
        if matched is not None:
            return matched
    normalized = _norm(label or "")
    if not normalized:
        return None
    return label_index.get(normalized)


def search_hpo_catalog(query: str, *, limit: int = 20) -> list[HpoCatalogEntry]:
    query = _norm(query)
    if not query:
        return []

    scored: list[tuple[int, HpoCatalogEntry]] = []
    for entry in load_hpo_catalog():
        code = _norm(entry.code)
        label = _norm(entry.label)
        chinese = _norm(entry.chinese_label)
        haystack = f"{code} {label} {chinese}"
        if query == code:
            score = 100
        elif query in code:
            score = 90
        elif query in label:
            score = 80
        elif query in chinese:
            score = 80
        elif all(part in haystack for part in query.split()):
            score = 60
        else:
            continue
        scored.append((score, entry))

    scored.sort(key=lambda item: (-item[0], item[1].code))
    return [entry for _, entry in scored[:limit]]
