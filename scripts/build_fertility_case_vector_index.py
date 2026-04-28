"""Build a local quantized vector index for fertility case retrieval.

This intentionally avoids a heavyweight vector database for the MVP. It builds
local text embeddings with TF-IDF character n-grams plus SVD, normalizes them,
and stores float16 vectors in an NPZ file.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.decomposition import TruncatedSVD
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import Normalizer


DEFAULT_PUBLIC = Path("database/fertility_public_cases_rds.csv")
DEFAULT_PRIVATE = Path("database/fertility_private_testing_cases_2025.with_hpo.csv")
DEFAULT_INDEX = Path("database/fertility_case_vector_index.npz")
DEFAULT_METADATA = Path("database/fertility_case_vector_metadata.csv")
DEFAULT_VECTORIZER = Path("database/fertility_case_vectorizer.joblib")
DEFAULT_STATS = Path("database/fertility_case_vector_index.stats.json")


def _cell(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return " ".join(str(value).split())


def _join(row: pd.Series, columns: list[str]) -> str:
    return " ".join(_cell(row.get(column, "")) for column in columns if _cell(row.get(column, "")))


def _public_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    df = pd.read_csv(path)
    rows: list[dict[str, str]] = []
    for _, row in df.iterrows():
        case_id = _cell(row.get("_id", ""))
        if not case_id:
            continue
        text = _join(
            row,
            [
                "case_report",
                "diagnosis",
                "Orpha_name",
                "matched_terms",
                "matched_categories",
            ],
        )
        if not text:
            continue
        rows.append(
            {
                "bank_type": "public",
                "case_id": case_id,
                "evidence_role": "diagnosis_reference",
                "text": text,
            }
        )
    return rows


def _private_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    df = pd.read_csv(path)
    rows: list[dict[str, str]] = []
    for _, row in df.iterrows():
        case_id = _cell(row.get("_id", ""))
        if not case_id:
            continue
        text = _join(
            row,
            [
                "case_report",
                "diagnosis",
                "clinical_suspected_diagnosis",
                "hpo_labels",
                "hpo_terms",
                "retrieval_tags",
                "reported_genes",
                "phenotype_relevant_genes",
                "variant_summary",
            ],
        )
        if not text:
            continue
        rows.append(
            {
                "bank_type": "private",
                "case_id": case_id,
                "evidence_role": "testing_finding_reference",
                "text": text,
            }
        )
    return rows


def build_index(
    public_path: Path,
    private_path: Path,
    index_path: Path,
    metadata_path: Path,
    vectorizer_path: Path,
    stats_path: Path,
    max_features: int,
    svd_components: int,
) -> dict[str, object]:
    rows = _public_rows(public_path) + _private_rows(private_path)
    if not rows:
        raise ValueError("No cases found for vector index building.")

    metadata = pd.DataFrame(rows)
    texts = metadata["text"].fillna("").astype(str).tolist()

    vectorizer = TfidfVectorizer(
        analyzer="char_wb",
        ngram_range=(2, 5),
        min_df=2,
        max_features=max_features,
        sublinear_tf=True,
    )
    tfidf = vectorizer.fit_transform(texts)
    n_components = min(svd_components, max(2, tfidf.shape[0] - 1), max(2, tfidf.shape[1] - 1))
    pipeline = Pipeline(
        [
            ("vectorizer", vectorizer),
            ("svd", TruncatedSVD(n_components=n_components, random_state=42)),
            ("normalizer", Normalizer(copy=False)),
        ]
    )
    vectors = pipeline.fit_transform(texts).astype(np.float32)
    vectors = np.nan_to_num(vectors, nan=0.0, posinf=0.0, neginf=0.0)
    metadata = metadata.drop(columns=["text"])
    metadata["vector_row"] = range(len(metadata))

    index_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(index_path, vectors=vectors.astype(np.float16))
    metadata.to_csv(metadata_path, index=False)
    joblib.dump(pipeline, vectorizer_path)

    stats = {
        "total_cases": int(len(metadata)),
        "public_cases": int((metadata["bank_type"] == "public").sum()),
        "private_cases": int((metadata["bank_type"] == "private").sum()),
        "tfidf_features": int(tfidf.shape[1]),
        "embedding_dimensions": int(vectors.shape[1]),
        "index_dtype": "float16",
        "index_path": str(index_path),
        "metadata_path": str(metadata_path),
        "vectorizer_path": str(vectorizer_path),
        "sanity_self_similarity": float(cosine_similarity(vectors[:1], vectors[:1])[0, 0]),
    }
    stats_path.write_text(json.dumps(stats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return stats


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public", type=Path, default=DEFAULT_PUBLIC)
    parser.add_argument("--private", type=Path, default=DEFAULT_PRIVATE)
    parser.add_argument("--index", type=Path, default=DEFAULT_INDEX)
    parser.add_argument("--metadata", type=Path, default=DEFAULT_METADATA)
    parser.add_argument("--vectorizer", type=Path, default=DEFAULT_VECTORIZER)
    parser.add_argument("--stats", type=Path, default=DEFAULT_STATS)
    parser.add_argument("--max-features", type=int, default=50000)
    parser.add_argument("--svd-components", type=int, default=256)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    stats = build_index(
        public_path=args.public,
        private_path=args.private,
        index_path=args.index,
        metadata_path=args.metadata,
        vectorizer_path=args.vectorizer,
        stats_path=args.stats,
        max_features=args.max_features,
        svd_components=args.svd_components,
    )
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
