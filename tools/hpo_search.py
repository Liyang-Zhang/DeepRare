from __future__ import annotations

import time

import requests


HPO_ASSOCIATION_API = "https://ontology.jax.org/api/network/annotation/{hpo_code}"
DEFAULT_TIMEOUT_SECONDS = 20
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_RETRY_DELAY_SECONDS = 1.0


def _normalize_hpo_code(query: str) -> str:
    text = str(query or "").strip().upper()
    if text.startswith("HP:"):
        return text
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) == 7:
        return f"HP:{digits}"
    raise ValueError(f"Invalid HPO code: {query}")


def _fetch_annotation(hpo_code: str) -> dict:
    last_error: Exception | None = None
    for attempt in range(1, DEFAULT_MAX_ATTEMPTS + 1):
        try:
            response = requests.get(
                HPO_ASSOCIATION_API.format(hpo_code=hpo_code),
                headers={"accept": "application/json"},
                timeout=DEFAULT_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            return response.json() or {}
        except requests.RequestException as exc:
            last_error = exc
            if attempt >= DEFAULT_MAX_ATTEMPTS:
                break
            time.sleep(DEFAULT_RETRY_DELAY_SECONDS * attempt)
    assert last_error is not None
    raise last_error


def HPOSearchTool(args, query: str) -> list[str]:
    """Return disease rows associated with one HPO code via the HPO public API."""
    hpo_code = _normalize_hpo_code(query)
    payload = _fetch_annotation(hpo_code)

    rows: list[str] = []
    for item in payload.get("diseases", []) or []:
        disease_id = str(item.get("id") or "").strip()
        disease_name = str(item.get("name") or "").strip()
        if not disease_id and not disease_name:
            continue
        if disease_id and disease_name:
            rows.append(f"{disease_id} {disease_name}")
        else:
            rows.append(disease_id or disease_name)
    return rows


if __name__ == "__main__":
    print(HPOSearchTool(None, "HP:0008222"))
