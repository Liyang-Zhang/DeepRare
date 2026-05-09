"""Default DeepRare-style placeholder services for the clinical MVP."""

from __future__ import annotations

import ast
import json
import os
import re
import time
from pathlib import Path
from typing import ClassVar
import urllib.error
import urllib.parse
import urllib.request
from xml.etree import ElementTree
from difflib import SequenceMatcher
from dataclasses import dataclass, field
from types import SimpleNamespace

from yk_ferta.schemas.clinical import CandidateCondition, PatientProfile, PhenotypeItem
from yk_ferta.schemas.evidence import CandidateReview, EvidenceItem, TraceableRecommendation
from yk_ferta.schemas.mvp import NormalizedDisease, PhenotypeToolHit, PhenotypeToolRun, SimilarCase
from yk_ferta.services.hpo_catalog import lookup_hpo_catalog


def _safe_json_loads(text: str) -> object | None:
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass

    if "```json" in text:
        parts = text.split("```json", 1)[1].split("```", 1)
        if parts:
            try:
                return json.loads(parts[0].strip())
            except Exception:
                pass

    for opener, closer in [("[", "]"), ("{", "}")]:
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end != -1 and end > start:
            snippet = text[start : end + 1]
            try:
                return json.loads(snippet)
            except Exception:
                continue
    return None


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip())


def _normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _contains_cjk(value: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", value or ""))


def _contains_latin_text(value: str) -> bool:
    return bool(re.search(r"[A-Za-z]", value or ""))


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _truncate(text: str, limit: int) -> str:
    text = _normalize_text(text)
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


_MOLECULAR_EVIDENCE_RESULT_RE = re.compile(
    r"("
    r"检出|携带|阳性|致病|疑似致病|变异|突变|"
    r"pathogenic|likely\s+pathogenic|variant|mutation|"
    r"\bc\.\d|p\.[A-Z][a-z]{2}|heterozygous|homozygous|compound\s+heterozygous"
    r")",
    re.IGNORECASE,
)
_MOLECULAR_TEST_CONTEXT_RE = re.compile(
    r"(基因|测序|检测|panel|wes|wgs|exome|genetic|sequenc|variant|mutation|cnv)",
    re.IGNORECASE,
)
_MOLECULAR_ASSERTION_RE = re.compile(
    r"("
    r"\bdue to\b|\bcaused by\b|\bsecondary to\b|"
    r"\b[A-Z0-9]{2,10}[- ]?related\b|"
    r"\b[A-Z0-9]{2,10}\s+(mutation|mutations|variant|variants)\b|"
    r"基因突变导致|突变导致|变异导致"
    r")",
    re.IGNORECASE,
)
_UNCONFIRMED_MOLECULAR_CLAIM_NOTE = (
    "当前病例未提供患者本人的基因/变异检测结果，因此任何基因层面的病因判断"
    "都只能作为未确认的分子假设，不能作为已确认病因。"
)


def _patient_has_molecular_evidence(patient: PatientProfile) -> bool:
    """Return True only when the current patient note appears to contain variant results."""
    narrative = patient.narrative()
    metadata_text = " ".join(str(value) for value in patient.metadata.values())
    text = f"{narrative}\n{metadata_text}".strip()
    if not text:
        return False
    return bool(
        _MOLECULAR_EVIDENCE_RESULT_RE.search(text)
        and _MOLECULAR_TEST_CONTEXT_RE.search(text)
    )


def _contains_molecular_assertion(text: str) -> bool:
    return bool(_MOLECULAR_ASSERTION_RE.search(text or ""))


def _molecular_evidence_policy(has_patient_molecular_evidence: bool) -> str:
    if has_patient_molecular_evidence:
        return (
            "当前病例似乎包含患者本人的分子检测/变异结果。可以讨论基因层面的病因，"
            "但必须区分已经检出的变异、疑似分子机制和建议进一步检测的内容。"
        )
    return (
        "当前病例没有提供患者本人的基因或变异检测结果。不要把诊断表述为已经确认"
        "由某个特定基因或突变导致。优先使用临床综合征/疾病名称；基因只能作为可能"
        "的分子病因或建议检测目标来描述。不要使用“分子亚型”概念。"
    )


def _soften_unconfirmed_molecular_candidate(
            name: str,
            rationale: str,
            has_patient_molecular_evidence: bool,
) -> tuple[str, str]:
    """Downgrade gene-causal wording when the patient has no molecular result."""
    name = _normalize_text(name)
    rationale = _normalize_text(rationale)
    if has_patient_molecular_evidence or not _contains_molecular_assertion(name):
        return name, rationale

    # In phenotype-only reasoning, prefer syndrome/disease-level names over
    # gene-first candidate names such as "GENE-related X".
    softened = re.sub(
        r"^\s*[A-Z0-9-]{2,15}[- ]related\s+",
        "",
        name,
        flags=re.IGNORECASE,
    )
    softened = re.sub(
        r"^\s*[A-Z0-9-]{2,15}\s*相关",
        "",
        softened,
        flags=re.IGNORECASE,
    )
    softened = re.sub(
        r"\s+(due to|caused by|secondary to)\s+[^()，,;；]+?(mutations?|variants?|突变|变异)",
        "",
        softened,
        flags=re.IGNORECASE,
    )
    softened = re.sub(
        r"\s*\(([^)]*(mutations?|variants?|突变|变异)[^)]*)\)",
        "",
        softened,
        flags=re.IGNORECASE,
    )
    softened = re.sub(r"\s*\([A-Z]{2,}\d+[A-Z0-9-]*\)\s*$", "", softened)
    softened = _normalize_text(softened).strip(" -:：;,，；")
    if not softened:
        softened = name
    if "分子病因未确认" not in softened and "molecular etiology unconfirmed" not in softened.lower():
        softened = f"{softened}（分子病因未确认）"

    if _UNCONFIRMED_MOLECULAR_CLAIM_NOTE not in rationale:
        rationale = f"{_UNCONFIRMED_MOLECULAR_CLAIM_NOTE} {rationale}".strip()
    return softened, rationale


class _OpenAIReasoner:
    def __init__(
        self,
        api_key: str,
        model_name: str,
        base_url: str = "",
        *,
        timeout: int = 60,
    ) -> None:
        from api.interface import Openai_api

        self._api = Openai_api(api_key, model_name, base_url=base_url, timeout=timeout)

    def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float | None = None,
        seed: int = 42,
    ) -> str | None:
        return self._api.get_completion(
            system_prompt,
            user_prompt,
            seed=seed,
            temperature=temperature,
        )


class NarrativePhenotypeExtractor:
    """Placeholder phenotype extractor for clinical-note-only MVP work."""

    def extract(self, patient: PatientProfile) -> list[PhenotypeItem]:
        narrative = patient.narrative()
        if not narrative:
            return []
        return [
            PhenotypeItem(
                label="临床叙述表型占位",
                source="mvp-placeholder",
                confidence=0.1,
                notes=narrative[:500],
            )
        ]


@dataclass(slots=True)
class RagHpoPhenotypeExtractor:
    """Adapter for the local task-based RAG-HPO extraction service."""

    base_url: str = "http://127.0.0.1:18080"
    temperature: float = 0.3
    enable_infertility_filter: bool = False
    request_timeout_seconds: int = 30
    poll_interval_seconds: float = 1.0
    poll_timeout_seconds: int = 120
    fallback_extractor: object | None = None

    def _api_url(self, path: str) -> str:
        return self.base_url.rstrip("/") + path

    def _request_json(
        self,
        url: str,
        *,
        method: str = "GET",
        payload: dict | None = None,
    ) -> dict:
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(request, timeout=self.request_timeout_seconds) as response:
            return json.load(response)

    def _create_task(self, patient: PatientProfile) -> dict:
        payload = {
            "input": {
                "case_id": patient.patient_id,
                "project_context": {"is_couple_project": False},
                "narrative": patient.narrative(),
                "spouse_narrative": "",
            },
            "options": {
                "temperature": self.temperature,
                "enable_infertility_filter": self.enable_infertility_filter,
            },
        }
        return self._request_json(
            self._api_url("/api/v1/hpo-extraction/tasks"),
            method="POST",
            payload=payload,
        )

    def _wait_for_task(self, task_id: str) -> dict:
        status_url = self._api_url(f"/api/v1/hpo-extraction/tasks/{task_id}")
        started = time.monotonic()
        while True:
            status = self._request_json(status_url)
            if status.get("status") in {"success", "partial", "failed"}:
                return status
            if time.monotonic() - started > self.poll_timeout_seconds:
                raise TimeoutError(f"RAG-HPO task {task_id} timed out")
            time.sleep(self.poll_interval_seconds)

    def _fetch_result(self, task_id: str) -> dict:
        return self._request_json(self._api_url(f"/api/v1/hpo-extraction/tasks/{task_id}/result"))

    def _result_to_phenotypes(self, result: dict) -> list[PhenotypeItem]:
        phenotypes: list[PhenotypeItem] = []
        payload = result.get("result") or {}
        persons = payload.get("persons") or []
        for person in persons:
            if person.get("person_role") != "self":
                continue
            for item in person.get("phenotypes") or []:
                catalog_entry = lookup_hpo_catalog(
                    code=item.get("hpo_id"),
                    label=item.get("hpo_name") or item.get("chpo_name") or item.get("phenotype_name"),
                )
                label = (
                    item.get("hpo_name")
                    or (catalog_entry.label if catalog_entry else "")
                    or item.get("chpo_name")
                    or item.get("phenotype_name")
                )
                chinese_label = item.get("chpo_name") or (catalog_entry.chinese_label if catalog_entry else "")
                if not label and not chinese_label:
                    continue
                notes_parts = [part for part in [item.get("phenotype_name"), item.get("parse_reason")] if part]
                phenotypes.append(
                    PhenotypeItem(
                        label=str(label or chinese_label),
                        chinese_label=str(chinese_label or ""),
                        code=item.get("hpo_id"),
                        source="rag-hpo-service",
                        confidence=_safe_float(item.get("similarity"), default=0.0) or None,
                        notes=" | ".join(notes_parts),
                    )
                )
        unique: list[PhenotypeItem] = []
        seen: set[tuple[str, str | None]] = set()
        for phenotype in phenotypes:
            key = (_normalize_key(phenotype.label), phenotype.code)
            if key in seen:
                continue
            seen.add(key)
            unique.append(phenotype)
        return unique

    def extract(self, patient: PatientProfile) -> list[PhenotypeItem]:
        narrative = patient.narrative()
        if not narrative:
            return []
        try:
            task = self._create_task(patient)
            task_view = self._wait_for_task(task["task_id"])
            if task_view.get("status") == "failed":
                raise RuntimeError(task_view.get("error") or "RAG-HPO task failed")
            result = self._fetch_result(task["task_id"])
            phenotypes = self._result_to_phenotypes(result)
            if phenotypes:
                return phenotypes
            raise RuntimeError("RAG-HPO returned no phenotypes")
        except Exception:
            if self.fallback_extractor is not None:
                return self.fallback_extractor.extract(patient)
            return []


@dataclass(slots=True)
class DeepRarePhenotypeExtractor:
    """DeepRare-style phenotype extractor using OpenAI + BioLORD HPO mapping."""

    api_key: str
    base_url: str = ""
    model_name: str = "gpt-4.1"
    biolord_model_path: str = "FremyCompany/BioLORD-2023-C"
    concept2id_path: str = "./database/definition2id.json"
    concept_embeddings_path: str = "./database/embeds_pheno.pt"
    similarity_threshold: float = 0.8
    _api: object | None = None
    _hpo_model: object | None = None
    _hpo_tokenizer: object | None = None
    _concept2id: dict | None = None
    _concept_embeddings: object | None = None
    _concept_keys: list[str] | None = None

    def __post_init__(self) -> None:
        """Keep initialization light; heavy resources load on first use."""

    @classmethod
    def from_environment(cls) -> "DeepRarePhenotypeExtractor | None":
        """Build the extractor from environment variables when possible."""
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            return None

        return cls(
            api_key=api_key,
            base_url=os.getenv("OPENAI_BASE_URL", "").strip(),
            model_name=os.getenv("YK_FERTA_PHENO_MODEL", "gpt-4.1"),
            biolord_model_path=os.getenv(
                "YK_FERTA_BIOLORD_MODEL",
                "FremyCompany/BioLORD-2023-C",
            ),
            concept2id_path=os.getenv(
                "YK_FERTA_HPO_CONCEPT2ID",
                "./database/definition2id.json",
            ),
            concept_embeddings_path=os.getenv(
                "YK_FERTA_HPO_EMBEDDINGS",
                "./database/embeds_pheno.pt",
            ),
            similarity_threshold=float(
                os.getenv("YK_FERTA_HPO_SIMILARITY_THRESHOLD", "0.8")
            ),
        )

    def _ensure_resources_loaded(self) -> None:
        """Lazily load DeepRare HPO extraction resources once."""
        if self._api is not None:
            return

        from api.interface import Openai_api
        from hpo_extractor import load_hpo_resources

        self._api = Openai_api(self.api_key, self.model_name, base_url=self.base_url)
        (
            self._hpo_model,
            self._hpo_tokenizer,
            self._concept2id,
            self._concept_embeddings,
            self._concept_keys,
        ) = load_hpo_resources(
            model_path=self.biolord_model_path,
            concept2id_path=self.concept2id_path,
            concept_embeddings_path=self.concept_embeddings_path,
        )

    def _extract_with_deeprare_prompt(self, narrative: str) -> list[str]:
        """Lightweight fallback that reuses the DeepRare extraction prompt only."""
        from api.interface import Openai_api

        if self._api is None:
            self._api = Openai_api(self.api_key, self.model_name, base_url=self.base_url)

        system_prompt = (
            "You are a medical expert specialized in rare disease and phenotype extraction."
        )
        prompt = (
            "Given a paragraph of patient information from discharge note, please extract "
            "the phenotype about this patient only. Check the Human Phenotype Ontology "
            "(HPO) database to determine the phenotype. Only output the extracted "
            "phenotypes. Use the format: {'HPO': 'HP:0000000', 'Phenotype': "
            "'Phenotype description'} Use \\n as the separator between different "
            "phenotypes. Please describe in English. Do not output any other information. "
            f"Patient information: {narrative}"
        )
        response = self._api.get_completion(system_prompt, prompt)
        if not response:
            return []

        extracted: list[str] = []
        for line in response.split("\n"):
            line = line.strip()
            if not (line.startswith("{") and line.endswith("}")):
                continue
            try:
                item = ast.literal_eval(line)
            except Exception:
                continue
            if isinstance(item, dict) and item.get("Phenotype"):
                extracted.append(str(item["Phenotype"]))
        return extracted

    def extract(self, patient: PatientProfile) -> list[PhenotypeItem]:
        """Apply the DeepRare extraction and HPO mapping logic to a clinical note."""
        narrative = patient.narrative()
        if not narrative:
            return []

        try:
            from hpo_extractor import extract_phenotypes_from_text, map_phenotypes_to_hpo

            self._ensure_resources_loaded()
            extracted = extract_phenotypes_from_text(narrative, self._api)
            if not extracted:
                return []

            mappings = map_phenotypes_to_hpo(
                extracted,
                self._hpo_model,
                self._hpo_tokenizer,
                self._concept2id,
                self._concept_embeddings,
                self._concept_keys,
                self.similarity_threshold,
            )
        except Exception:
            extracted = self._extract_with_deeprare_prompt(narrative)
            mappings = []

        phenotypes: list[PhenotypeItem] = []
        for item in mappings:
            status = item.get("status", "")
            if status == "mapped":
                phenotypes.append(
                    PhenotypeItem(
                        label=item["hpo_term"],
                        code=item["hpo_code"],
                        source="deeprare-hpo-extractor",
                        confidence=float(item["similarity_score"]),
                        notes=item["original_phenotype"],
                    )
                )

        if phenotypes:
            return phenotypes

        return [
            PhenotypeItem(
                label=label,
                source="deeprare-llm-extractor",
                confidence=0.3,
                notes="No HPO term passed the similarity threshold.",
            )
            for label in extracted
        ]


@dataclass(slots=True)
class DeepRarePhenotypeAnalyser:
    """DeepRare-style phenotype tool stage using public phenotype services."""

    chrome_driver: str = "/usr/local/bin/chromedriver"
    visualize: bool = False
    results_folder: str = "result_new/yk_ferta"
    enable_pubcasefinder: bool = False
    enable_phenobrain: bool = True
    enable_hpo_association: bool = False
    hpo_association_top_n: int = 5

    def _build_run(
        self,
        *,
        source: str,
        query: list[str],
        started_at: float,
        raw_result: str = "",
        parsed_candidates: list[PhenotypeToolHit] | None = None,
        error: str = "",
    ) -> PhenotypeToolRun:
        candidates = parsed_candidates or []
        if error:
            status = "failed"
        elif raw_result or candidates:
            status = "success"
        else:
            status = "skipped"
        return PhenotypeToolRun(
            source=source,
            status=status,
            query=query,
            raw_result=raw_result,
            parsed_candidates=candidates,
            error=error,
            elapsed_ms=int((time.monotonic() - started_at) * 1000),
        )

    def _parse_ranked_text(self, source: str, text: str) -> list[PhenotypeToolHit]:
        if not text:
            return []
        if ":" in text:
            text = text.split(":", 1)[1]
        raw_items = self._split_ranked_items(text)
        candidates = []
        seen: set[str] = set()
        for rank, raw in enumerate(raw_items, start=1):
            cleaned = _normalize_text(raw)
            if not cleaned:
                continue
            disease_id = None
            match = re.search(r"(ORPHA:\d+|OMIM:\d+)", cleaned)
            if match:
                disease_id = match.group(1)
            name = re.sub(r"\s*\([^)]*\)\s*$", "", cleaned).strip()
            key = _normalize_key(name)
            if not key or key in seen:
                continue
            seen.add(key)
            candidates.append(
                PhenotypeToolHit(
                    source=source,
                    disease_name=name,
                    disease_id=disease_id,
                    score=max(0.0, 1.0 - (rank - 1) * 0.1),
                    notes=cleaned,
                )
            )
        return candidates

    def _split_ranked_items(self, text: str) -> list[str]:
        """Split ranked tool output without breaking disease names that contain commas."""
        identifier_matches = list(re.finditer(r"(?:ORPHA|OMIM):\d+", text))
        if not identifier_matches:
            return text.split(",")

        items: list[str] = []
        start = 0
        for match in identifier_matches:
            item = text[start : match.end()]
            if item.strip():
                items.append(item.strip(" ,"))
            start = match.end()
            if start < len(text) and text[start] == ")":
                if items:
                    items[-1] = f"{items[-1]})"
                start += 1
            while start < len(text) and text[start] in {",", " "}:
                start += 1

        tail = text[start:].strip(" ,")
        if tail:
            items.extend(part for part in tail.split(",") if part.strip())
        return items

    def _parse_hpo_rows(self, rows: list[str]) -> list[PhenotypeToolHit]:
        candidates: list[PhenotypeToolHit] = []
        seen: set[str] = set()
        for rank, row in enumerate(rows, start=1):
            cleaned = _normalize_text(row)
            if not cleaned:
                continue
            match = re.search(r"(ORPHA:\d+|OMIM:\d+)", cleaned)
            disease_id = match.group(1) if match else None
            name = cleaned
            if disease_id:
                name = cleaned.replace(disease_id, "").strip(" -:\t")
            key = _normalize_key(name)
            if not key or key in seen:
                continue
            seen.add(key)
            candidates.append(
                PhenotypeToolHit(
                    source="hpo_association",
                    disease_name=name,
                    disease_id=disease_id,
                    score=max(0.0, 1.0 - (rank - 1) * 0.1),
                    notes=cleaned,
                )
            )
        limit = max(1, int(self.hpo_association_top_n or 5))
        return candidates[:limit]

    def analyze(
        self,
        patient: PatientProfile,
        phenotypes: list[PhenotypeItem],
    ) -> list[PhenotypeToolHit]:
        hits, _tool_runs = self.analyze_with_details(patient, phenotypes)
        return hits

    def analyze_with_details(
        self,
        patient: PatientProfile,
        phenotypes: list[PhenotypeItem],
    ) -> tuple[list[PhenotypeToolHit], list[PhenotypeToolRun]]:
        hpo_codes = [item.code for item in phenotypes if item.code]
        phenotype_labels = [item.label for item in phenotypes if item.label]
        args = SimpleNamespace(
            results_folder=self.results_folder,
            chrome_driver=self.chrome_driver,
            visualize=self.visualize,
        )

        hits: list[PhenotypeToolHit] = []
        tool_runs: list[PhenotypeToolRun] = []

        if not self.enable_pubcasefinder:
            tool_runs.append(
                PhenotypeToolRun(
                    source="pubcasefinder",
                    status="skipped",
                    query=hpo_codes,
                    error="Disabled by phenotype_analyser.enable_pubcasefinder.",
                )
            )
        elif hpo_codes:
            started_at = time.monotonic()
            try:
                from tools.pubcase_finder import PubCaseFinderSearchTool

                raw_result = PubCaseFinderSearchTool(args, hpo_codes)
                parsed = self._parse_ranked_text("pubcasefinder", raw_result)
                hits.extend(parsed)
                tool_runs.append(
                    self._build_run(
                        source="pubcasefinder",
                        query=hpo_codes,
                        started_at=started_at,
                        raw_result=raw_result,
                        parsed_candidates=parsed,
                    )
                )
            except Exception as exc:
                tool_runs.append(
                    self._build_run(
                        source="pubcasefinder",
                        query=hpo_codes,
                        started_at=started_at,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
        else:
            tool_runs.append(
                PhenotypeToolRun(
                    source="pubcasefinder",
                    status="skipped",
                    query=[],
                    error="No HPO codes available.",
                )
            )

        if not self.enable_phenobrain:
            tool_runs.append(
                PhenotypeToolRun(
                    source="phenobrain",
                    status="skipped",
                    query=hpo_codes or phenotype_labels[:10],
                    error="Disabled by phenotype_analyser.enable_phenobrain.",
                )
            )
        else:
            started_at = time.monotonic()
            try:
                from tools.phenobrain_api import PhenobrainAPITool

                phenobrain_query: str | list[str]
                if hpo_codes:
                    phenobrain_query = hpo_codes
                else:
                    phenobrain_query = ", ".join(phenotype_labels[:10])
                raw_result = PhenobrainAPITool(phenobrain_query) or ""
                parsed = self._parse_ranked_text("phenobrain", raw_result)
                hits.extend(parsed)
                tool_runs.append(
                    self._build_run(
                        source="phenobrain",
                        query=hpo_codes or phenotype_labels[:10],
                        started_at=started_at,
                        raw_result=raw_result,
                        parsed_candidates=parsed,
                    )
                )
            except Exception as exc:
                tool_runs.append(
                    self._build_run(
                        source="phenobrain",
                        query=hpo_codes or phenotype_labels[:10],
                        started_at=started_at,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )

        if not self.enable_hpo_association:
            tool_runs.append(
                PhenotypeToolRun(
                    source="hpo_association",
                    status="skipped",
                    query=hpo_codes[:3],
                    error="Disabled by phenotype_analyser.enable_hpo_association.",
                )
            )
        elif hpo_codes:
            started_at = time.monotonic()
            from tools.hpo_search import HPOSearchTool

            rows: list[str] = []
            errors: list[str] = []
            for hpo_code in hpo_codes[:3]:
                try:
                    rows.extend(HPOSearchTool(args, hpo_code))
                except Exception as exc:
                    errors.append(f"{hpo_code}: {type(exc).__name__}: {exc}")

            if rows:
                parsed = self._parse_hpo_rows(rows)
                hits.extend(parsed)
                raw_parts = ["\n".join(rows)]
                if errors:
                    raw_parts.append("Partial failures:\n" + "\n".join(errors))
                tool_runs.append(
                    self._build_run(
                        source="hpo_association",
                        query=hpo_codes[:3],
                        started_at=started_at,
                        raw_result="\n\n".join(part for part in raw_parts if part.strip()),
                        parsed_candidates=parsed,
                    )
                )
            else:
                tool_runs.append(
                    self._build_run(
                        source="hpo_association",
                        query=hpo_codes[:3],
                        started_at=started_at,
                        error="; ".join(errors) or "No HPO association rows returned.",
                    )
                )
        else:
            tool_runs.append(
                PhenotypeToolRun(
                    source="hpo_association",
                    status="skipped",
                    query=[],
                    error="No HPO codes available.",
                )
            )

        unique: list[PhenotypeToolHit] = []
        seen: set[str] = set()
        for hit in sorted(hits, key=lambda item: item.score or 0.0, reverse=True):
            key = _normalize_key(hit.disease_name)
            if not key or key in seen:
                continue
            seen.add(key)
            unique.append(hit)
        return unique[:10], tool_runs


class StubPhenotypeAnalyser:
    """Placeholder phenotype analyzer mimicking phenotype-tool hints."""

    def analyze(
        self,
        patient: PatientProfile,
        phenotypes: list[PhenotypeItem],
    ) -> list[PhenotypeToolHit]:
        hits, _tool_runs = self.analyze_with_details(patient, phenotypes)
        return hits

    def analyze_with_details(
        self,
        patient: PatientProfile,
        phenotypes: list[PhenotypeItem],
    ) -> tuple[list[PhenotypeToolHit], list[PhenotypeToolRun]]:
        hits = [
            PhenotypeToolHit(
                source="phenotype-tool-placeholder",
                disease_name="需要接入表型驱动候选生成工具",
                score=0.2,
                notes="请替换为 PubCaseFinder、Phenobrain 或领域表型工具。",
            )
        ]
        return hits, [
            PhenotypeToolRun(
                source="phenotype-tool-placeholder",
                status="success",
                query=[item.code or item.label for item in phenotypes],
                raw_result=hits[0].notes,
                parsed_candidates=hits,
            )
        ]


class StubKnowledgeSearcher:
    """Placeholder knowledge searcher for local knowledge integration."""

    def search(
        self,
        patient: PatientProfile,
        phenotypes: list[PhenotypeItem],
    ) -> list[EvidenceItem]:
        return [
            EvidenceItem(
                source_id="knowledge-placeholder-001",
                source_type="guideline",
                title="知识证据占位",
                summary=(
                    "请替换为受控检索结果，例如本地指南、内部知识卡、结构化数据库和可选在线证据。"
                ),
                citation="MVP 占位证据源。",
            )
        ]


@dataclass(slots=True)
class DeepRareKnowledgeSearcher:
    """DeepRare-style online knowledge searcher with graceful fallbacks."""

    search_engine: str = "duckduckgo"
    google_api: str = ""
    search_engine_id: str = ""
    chrome_driver: str = "/usr/local/bin/chromedriver"
    visualize: bool = False
    openai_api_key: str = ""
    openai_base_url: str = ""
    mini_model_name: str = "gpt-4o-mini"
    web_results: int = 3
    pubmed_results: int = 2
    arxiv_results: int = 2
    wiki_results: int = 2
    _mini_handler: object | None = None
    _args: object | None = None

    @classmethod
    def from_environment(cls) -> "DeepRareKnowledgeSearcher | None":
        """Build the searcher when minimum online-search config is present."""
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            return None
        return cls(
            search_engine=os.getenv("YK_FERTA_SEARCH_ENGINE", "duckduckgo").strip().lower(),
            google_api=os.getenv("YK_FERTA_GOOGLE_API", "").strip(),
            search_engine_id=os.getenv("YK_FERTA_GOOGLE_CSE_ID", "").strip(),
            chrome_driver=os.getenv("YK_FERTA_CHROME_DRIVER", "/usr/local/bin/chromedriver"),
            visualize=os.getenv("YK_FERTA_VISUALIZE", "0").strip() == "1",
            openai_api_key=api_key,
            openai_base_url=os.getenv("OPENAI_BASE_URL", "").strip(),
            mini_model_name=os.getenv("YK_FERTA_MINI_MODEL", "gpt-4o-mini").strip(),
            web_results=int(os.getenv("YK_FERTA_WEB_RESULTS", "3")),
            pubmed_results=int(os.getenv("YK_FERTA_PUBMED_RESULTS", "2")),
            arxiv_results=int(os.getenv("YK_FERTA_ARXIV_RESULTS", "2")),
            wiki_results=int(os.getenv("YK_FERTA_WIKI_RESULTS", "2")),
        )

    def _ensure_clients(self) -> None:
        """Lazy-init the DeepRare-compatible helper objects."""
        if self._mini_handler is not None:
            return

        from api.interface import Openai_api

        self._mini_handler = Openai_api(
            self.openai_api_key,
            self.mini_model_name,
            base_url=self.openai_base_url,
        )
        self._args = SimpleNamespace(
            search_engine=self.search_engine,
            google_api=self.google_api,
            search_engine_id=self.search_engine_id,
            chrome_driver=self.chrome_driver,
            visualize=self.visualize,
        )

    def _build_query(
        self,
        patient: PatientProfile,
        phenotypes: list[PhenotypeItem],
    ) -> str:
        """Mirror DeepRare's tendency to search with phenotype text."""
        phenotype_labels = [item.label for item in phenotypes if item.label]
        if phenotype_labels:
            return ", ".join(phenotype_labels[:12])
        return patient.narrative()[:500]

    def _build_queries(
        self,
        patient: PatientProfile,
        phenotypes: list[PhenotypeItem],
    ) -> dict[str, str]:
        """Build source-specific queries instead of sending raw case text everywhere.

        DeepRare mostly uses phenotype text directly. That is a reasonable baseline,
        but exact comma-joined HPO labels often underperform in generic web search.
        This keeps the phenotype-first behavior while adding a small amount of
        domain-aware query shaping for fertility cases.
        """
        base_query = self._build_query(patient, phenotypes)
        labels = [item.label for item in phenotypes if item.label]
        label_text = " ".join(labels)
        narrative = patient.narrative()
        combined = f"{label_text} {narrative}".lower()

        web_query = " ".join(labels[:6]) if labels else base_query
        pubmed_query = base_query

        if "hydatidiform" in combined or "葡萄胎" in combined:
            web_query = '"recurrent hydatidiform mole" infertility'
            pubmed_query = (
                '("recurrent hydatidiform mole"[Title/Abstract] OR '
                '"hydatidiform mole"[Title/Abstract]) AND '
                '("infertility"[Title/Abstract] OR "recurrent pregnancy loss"[Title/Abstract] '
                'OR "female infertility"[Title/Abstract])'
            )
        elif "infertility" in combined or "不孕" in combined or "fertility" in combined:
            extra_terms = [label for label in labels if label.lower() not in {"infertility", "female infertility"}]
            if extra_terms:
                web_query = " ".join(["infertility", *extra_terms[:5]])
                pubmed_query = " AND ".join([f'"{term}"' for term in ["infertility", *extra_terms[:3]]])
            else:
                pubmed_query = '"infertility"[Title/Abstract]'

        return {
            "web": web_query or base_query,
            "pubmed": pubmed_query or base_query,
            "arxiv": base_query,
            "wikipedia": web_query or base_query,
        }

    @staticmethod
    def _is_useful_summary(summary: str) -> bool:
        if not summary or not summary.strip():
            return False
        lower = summary.lower()
        failure_markers = (
            "no results found",
            "error during",
            "failed:",
            "failed to",
            "object is not callable",
            "could not import",
            "traceback",
        )
        return not any(marker in lower for marker in failure_markers)

    def _append_evidence(
        self,
        evidence: list[EvidenceItem],
        source_id: str,
        source_type: str,
        title: str,
        summary: str,
        *,
        citation: str = "",
        url: str = "",
        relevance_score: float | None = None,
        metadata: dict[str, str] | None = None,
    ) -> None:
        if not self._is_useful_summary(summary):
            return
        evidence.append(
            EvidenceItem(
                source_id=source_id,
                source_type=source_type,
                title=title,
                summary=summary[:4000],
                citation=citation,
                url=url,
                relevance_score=relevance_score,
                metadata=metadata or {},
            )
        )

    @staticmethod
    def _xml_text(node: ElementTree.Element | None) -> str:
        if node is None:
            return ""
        return _normalize_text(" ".join(node.itertext()))

    def _search_pubmed_direct(self, query: str, max_results: int) -> list[EvidenceItem]:
        """Search PubMed through NCBI E-utilities.

        This replaces DeepRare's LangChain PubMedRetriever dependency path, which
        currently fails without xmltodict in the dev environment and is harder to
        control for evidence quality.
        """
        if max_results <= 0 or not query:
            return []

        search_params = urllib.parse.urlencode(
            {
                "db": "pubmed",
                "term": query,
                "retmode": "json",
                "retmax": str(max(max_results * 4, max_results)),
                "sort": "relevance",
            }
        )
        search_url = f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?{search_params}"
        request = urllib.request.Request(
            search_url,
            headers={"User-Agent": "yk-FERTA/0.1 (clinical MVP; PubMed evidence search)"},
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            search_payload = json.loads(response.read().decode("utf-8"))

        pmids = search_payload.get("esearchresult", {}).get("idlist", [])
        if not pmids:
            return []

        fetch_params = urllib.parse.urlencode(
            {
                "db": "pubmed",
                "id": ",".join(pmids),
                "retmode": "xml",
            }
        )
        fetch_url = f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?{fetch_params}"
        request = urllib.request.Request(
            fetch_url,
            headers={"User-Agent": "yk-FERTA/0.1 (clinical MVP; PubMed evidence fetch)"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            root = ElementTree.fromstring(response.read())

        evidence: list[EvidenceItem] = []
        for article in root.findall(".//PubmedArticle"):
            pmid = self._xml_text(article.find(".//PMID"))
            if not pmid:
                continue
            title = self._xml_text(article.find(".//ArticleTitle")) or f"PubMed article {pmid}"
            abstract_parts = [
                self._xml_text(node)
                for node in article.findall(".//Abstract/AbstractText")
                if self._xml_text(node)
            ]
            abstract = _normalize_text(" ".join(abstract_parts))
            journal = self._xml_text(article.find(".//Journal/Title"))
            year = self._xml_text(article.find(".//JournalIssue/PubDate/Year"))
            citation = " ".join(part for part in [f"PMID:{pmid}", journal, year] if part)
            score = self._score_pubmed_article(query, title, abstract)
            evidence.append(
                EvidenceItem(
                    source_id=f"pubmed-{pmid}",
                    source_type="pubmed",
                    title=title[:500],
                    summary=(abstract or title)[:2000],
                    citation=citation,
                    url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                    relevance_score=score,
                    metadata={"query": query},
                )
            )
        evidence.sort(key=lambda item: item.relevance_score or 0.0, reverse=True)
        return evidence[:max_results]

    def _search_web_ddgs(self, query: str, max_results: int) -> str:
        """Search general web snippets with the maintained ddgs package."""
        if max_results <= 0 or not query:
            return ""
        try:
            from ddgs import DDGS
        except Exception:
            return ""

        preferred_backend = self.search_engine if self.search_engine in {"duckduckgo", "brave", "google", "bing"} else ""
        backends = [backend for backend in [preferred_backend, "duckduckgo", "brave", "google", "bing"] if backend]
        seen_backends: set[str] = set()
        for backend in backends:
            if backend in seen_backends:
                continue
            seen_backends.add(backend)
            try:
                with DDGS() as ddgs:
                    results = list(ddgs.text(query, max_results=max_results, backend=backend))
            except Exception:
                continue
            if not results:
                continue
            chunks = []
            for result in results[:max_results]:
                title = _normalize_text(str(result.get("title", "")))
                body = _normalize_text(str(result.get("body", "")))
                href = _normalize_text(str(result.get("href", "")))
                if not title and not body:
                    continue
                chunks.append(
                    "\n".join(
                        line
                        for line in [
                            f"Title: {title}" if title else "",
                            f"Snippet: {body}" if body else "",
                            f"URL: {href}" if href else "",
                        ]
                        if line
                    )
                )
            if chunks:
                return "\n\n".join(chunks)
        return ""

    @staticmethod
    def _extract_relevance_terms(query: str) -> tuple[list[str], set[str]]:
        query_lower = query.lower()
        phrases = []
        for phrase in re.findall(r'"([^"]+)"', query_lower):
            cleaned = re.sub(r"\[[^\]]+\]", "", phrase).strip()
            if cleaned:
                phrases.append(cleaned)

        normalized = re.sub(r"\[[^\]]+\]", " ", query_lower)
        normalized = re.sub(r"\b(and|or|not)\b", " ", normalized)
        normalized = re.sub(r"[()\":]", " ", normalized)
        normalized = re.sub(r"\s+", " ", normalized).strip()
        stop_tokens = {
            "title",
            "abstract",
            "ti",
            "ab",
            "mesh",
            "jour",
            "dp",
            "au",
            "tiab",
        }
        tokens = {
            token
            for token in re.findall(r"[a-z0-9]+", normalized)
            if len(token) > 2 and token not in stop_tokens
        }
        return phrases, tokens

    @classmethod
    def _score_text_relevance(cls, query: str, title: str, body: str) -> float:
        phrases, query_tokens = cls._extract_relevance_terms(query)
        if not phrases and not query_tokens:
            return 0.0

        title_lower = title.lower()
        body_lower = body.lower()
        title_tokens = set(re.findall(r"[a-z0-9]+", title_lower))
        body_tokens = set(re.findall(r"[a-z0-9]+", body_lower))

        phrase_title = (
            sum(1 for phrase in phrases if phrase in title_lower) / len(phrases)
            if phrases else 0.0
        )
        phrase_body = (
            sum(1 for phrase in phrases if phrase in body_lower) / len(phrases)
            if phrases else 0.0
        )
        token_title = (
            len(query_tokens & title_tokens) / len(query_tokens)
            if query_tokens else 0.0
        )
        token_body = (
            len(query_tokens & body_tokens) / len(query_tokens)
            if query_tokens else 0.0
        )

        score = (
            0.45 * phrase_title
            + 0.20 * phrase_body
            + 0.25 * token_title
            + 0.10 * token_body
        )
        return round(max(0.0, min(1.0, score)), 4)

    @classmethod
    def _score_pubmed_article(cls, query: str, title: str, abstract: str) -> float:
        return cls._score_text_relevance(query, title, abstract)

    def search(
        self,
        patient: PatientProfile,
        phenotypes: list[PhenotypeItem],
    ) -> list[EvidenceItem]:
        """Run DeepRare-style online evidence search using phenotype text."""
        queries = self._build_queries(patient, phenotypes)
        if not any(queries.values()):
            return []

        self._ensure_clients()

        evidence: list[EvidenceItem] = []

        if self.web_results > 0:
            try:
                from tools.web_search import BingSearchTool, DuckDuckGoSearchTool, GoogleSearchTool

                web_query = queries["web"]
                web_summary = self._search_web_ddgs(web_query, self.web_results)
                if web_summary:
                    self._append_evidence(
                        evidence,
                        "web-ddgs",
                        "web_search",
                        "Web search results",
                        web_summary,
                        relevance_score=self._score_text_relevance(web_query, "Web search results", web_summary),
                        metadata={"query": web_query},
                    )
                elif self.search_engine == "google":
                    web_summary = GoogleSearchTool(
                        self._args,
                        web_query,
                        self._mini_handler,
                        read_content=False,
                        return_num=self.web_results,
                    )
                elif self.search_engine == "bing":
                    web_summary = BingSearchTool(
                        self._args,
                        web_query,
                        self._mini_handler,
                        read_content=False,
                        return_num=self.web_results,
                    )
                else:
                    web_summary = DuckDuckGoSearchTool(
                        self._args,
                        web_query,
                        read_content=False,
                        return_num=self.web_results,
                        mini_handler=self._mini_handler,
                    )
                    self._append_evidence(
                        evidence,
                        "web-search",
                        "web_search",
                        f"{self.search_engine} search results",
                        web_summary,
                        relevance_score=self._score_text_relevance(web_query, f"{self.search_engine} search results", web_summary),
                        metadata={"query": web_query},
                    )
            except Exception:
                # Online search is best-effort; failed source runs should not become
                # clinical evidence consumed by later LLM stages.
                pass

        try:
            evidence.extend(self._search_pubmed_direct(queries["pubmed"], self.pubmed_results))
        except Exception:
            pass

        if self.arxiv_results > 0:
            try:
                from tools.search_arxiv import search_Arxiv

                arxiv_summary = search_Arxiv(queries["arxiv"], self.arxiv_results, self._mini_handler)
                self._append_evidence(
                    evidence,
                    "arxiv-search",
                    "arxiv",
                    "ArXiv search results",
                    arxiv_summary,
                    relevance_score=self._score_text_relevance(queries["arxiv"], "ArXiv search results", arxiv_summary),
                    metadata={"query": queries["arxiv"]},
                )
            except Exception:
                pass

        if self.wiki_results > 0:
            try:
                from tools.search_wiki import search_Wiki

                wiki_summary = search_Wiki(queries["wikipedia"], self.wiki_results, self._mini_handler)
                self._append_evidence(
                    evidence,
                    "wiki-search",
                    "wikipedia",
                    "Wikipedia search results",
                    wiki_summary,
                    relevance_score=self._score_text_relevance(queries["wikipedia"], "Wikipedia search results", wiki_summary),
                    metadata={"query": queries["wikipedia"]},
                )
            except Exception:
                pass

        return evidence


class StubCaseSearcher:
    """Placeholder case searcher for the local case bank stage."""

    def search(
        self,
        patient: PatientProfile,
        phenotypes: list[PhenotypeItem],
    ) -> list[SimilarCase]:
        return [
            SimilarCase(
                case_id="case-bank-placeholder-001",
                source="local-case-bank-placeholder",
                summary="MVP 编排用相似病例占位摘要。",
                diagnosis="需要接入本地相似病例检索",
                score=0.2,
            )
        ]


@dataclass(slots=True)
class FertilityDualCaseSearcher:
    """Retrieve public diagnostic cases and private testing cases for fertility workflows."""

    public_case_bank_path: str = "./database/fertility_public_cases_rds.csv"
    private_testing_case_bank_path: str = "./database/fertility_private_testing_cases_2025.with_hpo.csv"
    vector_index_path: str = "./database/fertility_case_vector_index.npz"
    vector_metadata_path: str = "./database/fertility_case_vector_metadata.csv"
    vectorizer_path: str = "./database/fertility_case_vectorizer.joblib"
    public_return_k: int = 3
    private_return_k: int = 3
    vector_top_n: int = 200
    vector_weight: float = 0.45
    min_score: float = 0.01
    _public_bank: object | None = None
    _private_bank: object | None = None
    _public_by_id: object | None = None
    _private_by_id: object | None = None
    _vectors: object | None = None
    _vector_metadata: object | None = None
    _vectorizer: object | None = None

    _tag_rules = {
        "infertility": ["infertility", "subfertility", "sterility", "不孕", "不育"],
        "poi": [
            "primary ovarian insufficiency",
            "premature ovarian insufficiency",
            "premature ovarian failure",
            "ovarian insufficiency",
            "卵巢早衰",
            "早发性卵巢功能不全",
        ],
        "oocyte_maturation": ["oocyte maturation", "卵母细胞成熟", "卵子成熟"],
        "fallopian_tube": ["fallopian tube", "输卵管"],
        "male_factor": [
            "azoospermia",
            "oligozoospermia",
            "teratozoospermia",
            "sperm",
            "精子",
            "少精",
            "弱精",
            "畸精",
            "无精",
        ],
        "dsd": ["disorders of sex development", "dsd", "性发育异常", "两性畸形"],
        "recurrent_pregnancy_loss": [
            "recurrent pregnancy loss",
            "recurrent miscarriage",
            "复发性流产",
            "反复流产",
        ],
        "molar_pregnancy": ["hydatidiform mole", "molar pregnancy", "葡萄胎"],
        "embryo": ["embryo", "ivf", "implantation", "胚胎", "着床", "试管"],
    }
    _specific_tags = {
        "poi",
        "oocyte_maturation",
        "fallopian_tube",
        "male_factor",
        "dsd",
        "recurrent_pregnancy_loss",
        "molar_pregnancy",
    }

    def _ensure_loaded(self) -> None:
        if self._public_bank is None or self._private_bank is None:
            import pandas as pd

            if os.path.exists(self.public_case_bank_path):
                self._public_bank = pd.read_csv(self.public_case_bank_path)
                self._public_by_id = self._public_bank.set_index("_id", drop=False)
            else:
                self._public_bank = pd.DataFrame()
                self._public_by_id = pd.DataFrame()

            if os.path.exists(self.private_testing_case_bank_path):
                self._private_bank = pd.read_csv(self.private_testing_case_bank_path)
                self._private_by_id = self._private_bank.set_index("_id", drop=False)
            else:
                self._private_bank = pd.DataFrame()
                self._private_by_id = pd.DataFrame()

        if self._vectors is None and self._vector_metadata is None and self._vectorizer is None:
            if (
                os.path.exists(self.vector_index_path)
                and os.path.exists(self.vector_metadata_path)
                and os.path.exists(self.vectorizer_path)
            ):
                try:
                    import joblib
                    import numpy as np
                    import pandas as pd

                    self._vectors = np.load(self.vector_index_path)["vectors"].astype("float32")
                    self._vector_metadata = pd.read_csv(self.vector_metadata_path)
                    self._vectorizer = joblib.load(self.vectorizer_path)
                except Exception:
                    self._vectors = None
                    self._vector_metadata = None
                    self._vectorizer = None

    @staticmethod
    def _cell(value: object) -> str:
        if value is None:
            return ""
        try:
            import pandas as pd

            if pd.isna(value):
                return ""
        except Exception:
            pass
        return _normalize_text(str(value))

    @staticmethod
    def _split_pipe(value: object) -> list[str]:
        text = FertilityDualCaseSearcher._cell(value)
        if not text:
            return []
        return [item.strip() for item in text.split("|") if item.strip()]

    def _tokenize(self, text: str) -> set[str]:
        text_lower = text.lower()
        tokens = {
            token
            for token in re.findall(r"hp:\d+|[a-z0-9]+", text_lower)
            if len(token) > 1
        }
        for terms in self._tag_rules.values():
            for term in terms:
                if term and term.lower() in text_lower:
                    tokens.add(term.lower())
        return tokens

    def _infer_tags(self, text: str) -> set[str]:
        text_lower = text.lower()
        tags: set[str] = set()
        for tag, terms in self._tag_rules.items():
            for term in terms:
                term_lower = term.lower()
                if not term_lower:
                    continue
                if re.fullmatch(r"[a-z0-9]+", term_lower):
                    if re.search(rf"\b{re.escape(term_lower)}\b", text_lower):
                        tags.add(tag)
                elif term_lower in text_lower:
                    tags.add(tag)
        return tags

    def _extract_genes(self, text: str) -> set[str]:
        genes: set[str] = set()
        for token in re.findall(r"\b[A-Z][A-Z0-9]{1,12}\b", text):
            if token.startswith("HP") or token in {"IVF", "ICSI", "DNA", "RNA", "WES"}:
                continue
            genes.add(token)
        return genes

    def _query_context(
        self,
        patient: PatientProfile,
        phenotypes: list[PhenotypeItem],
    ) -> dict[str, object]:
        phenotype_text = " ".join(
            " ".join(part for part in [item.label, item.code or ""] if part)
            for item in phenotypes
        )
        query = _normalize_text(f"{patient.narrative()} {phenotype_text}")
        return {
            "text": query,
            "tokens": self._tokenize(query),
            "tags": self._infer_tags(query),
            "genes": self._extract_genes(query),
            "hpo_codes": {item.code.lower() for item in phenotypes if item.code},
        }

    def _base_overlap_score(self, query_tokens: set[str], text: str) -> float:
        case_tokens = self._tokenize(text)
        if not query_tokens or not case_tokens:
            return 0.0
        overlap = len(query_tokens & case_tokens)
        return min(0.45, 0.45 * overlap / max(1, len(query_tokens)))

    def _score_public_row(self, row: object, context: dict[str, object]) -> float:
        query_tokens = context["tokens"]
        query_tags = context["tags"]
        text = " ".join(
            self._cell(row.get(column, ""))
            for column in [
                "case_report",
                "diagnosis",
                "Orpha_name",
                "matched_terms",
                "matched_categories",
            ]
        )
        score = self._base_overlap_score(query_tokens, text)  # type: ignore[arg-type]
        row_categories = set(self._split_pipe(row.get("matched_categories", "")))
        row_terms = " ".join(self._split_pipe(row.get("matched_terms", "")))
        row_tags = self._infer_tags(f"{row_terms} {' '.join(row_categories)} {text}")
        tag_overlap = len(query_tags & row_tags)  # type: ignore[operator]
        if (query_tags & self._specific_tags) and not (query_tags & row_tags & self._specific_tags):  # type: ignore[operator]
            return 0.0
        score += min(0.35, 0.12 * tag_overlap)
        tier = self._cell(row.get("fertility_relevance_tier", "")).lower()
        if tier == "strong":
            score += 0.12
        elif tier == "moderate":
            score += 0.07
        elif tier == "weak":
            score += 0.03
        return round(score, 4)

    def _score_private_row(self, row: object, context: dict[str, object]) -> float:
        query_tokens = context["tokens"]
        query_tags = context["tags"]
        query_genes = context["genes"]
        query_hpos = context["hpo_codes"]
        text = " ".join(
            self._cell(row.get(column, ""))
            for column in [
                "case_report",
                "diagnosis",
                "clinical_suspected_diagnosis",
                "hpo_labels",
                "hpo_terms",
                "retrieval_tags",
                "reported_genes",
                "phenotype_relevant_genes",
                "variant_summary",
            ]
        )
        score = self._base_overlap_score(query_tokens, text)  # type: ignore[arg-type]
        row_hpos = {item.lower() for item in self._split_pipe(row.get("hpo_terms", ""))}
        hpo_overlap = len(query_hpos & row_hpos)  # type: ignore[operator]
        score += min(0.5, 0.18 * hpo_overlap)
        row_tags = set(self._split_pipe(row.get("retrieval_tags", ""))) | self._infer_tags(text)
        tag_overlap = len(query_tags & row_tags)  # type: ignore[operator]
        if (query_tags & self._specific_tags) and not (query_tags & row_tags & self._specific_tags):  # type: ignore[operator]
            return 0.0
        score += min(0.3, 0.1 * tag_overlap)
        row_genes = set(self._split_pipe(row.get("reported_genes", ""))) | set(
            self._split_pipe(row.get("phenotype_relevant_genes", ""))
        )
        gene_overlap = len(query_genes & row_genes)  # type: ignore[operator]
        score += min(0.35, 0.18 * gene_overlap)
        if _safe_float(row.get("phenotype_relevant_variant_count"), 0.0) > 0:
            score += 0.08
        if self._cell(row.get("data_quality", "")).lower() == "high":
            score += 0.03
        return round(score, 4)

    def _public_case_from_row(
        self,
        row: object,
        score: float,
        vector_score: float = 0.0,
    ) -> SimilarCase:
        metadata = {
            "evidence_role": "diagnosis_reference",
            "source_dataset": self._cell(row.get("source_dataset", "")),
            "source_record_id": self._cell(row.get("source_record_id", "")),
            "source_pub_date": self._cell(row.get("source_pub_date", "")),
            "source_pmid": self._cell(row.get("source_pmid", "")),
            "source_title": self._cell(row.get("source_title", "")),
            "source_file_path": self._cell(row.get("source_file_path", "")),
            "source_url": self._cell(row.get("source_url", "")),
            "matched_terms": self._cell(row.get("matched_terms", "")),
            "matched_categories": self._cell(row.get("matched_categories", "")),
            "fertility_relevance_tier": self._cell(row.get("fertility_relevance_tier", "")),
            "vector_score": f"{vector_score:.4f}" if vector_score > 0 else "",
        }
        disease_id = self._cell(row.get("Orpha_id", ""))
        if disease_id and not disease_id.startswith("ORPHA:"):
            disease_id = f"ORPHA:{disease_id}"
        return SimilarCase(
            case_id=self._cell(row.get("_id", "")),
            source=f"public-case-bank:{'hybrid' if vector_score > 0 else 'lexical'}",
            summary=self._cell(row.get("case_report", ""))[:2000],
            diagnosis=self._cell(row.get("diagnosis", "")) or self._cell(row.get("Orpha_name", "")),
            score=score,
            evidence_role="diagnosis_reference",
            disease_id=disease_id,
            metadata=metadata,
        )

    def _private_case_from_row(
        self,
        row: object,
        score: float,
        vector_score: float = 0.0,
    ) -> SimilarCase:
        reported_genes = self._split_pipe(row.get("reported_genes", ""))
        phenotype_relevant_genes = self._split_pipe(row.get("phenotype_relevant_genes", ""))
        metadata = {
            "evidence_role": "testing_finding_reference",
            "project_id": self._cell(row.get("project_id", "")),
            "test_project": self._cell(row.get("test_project", "")),
            "report_status": self._cell(row.get("report_status", "")),
            "retrieval_tags": self._cell(row.get("retrieval_tags", "")),
            "hpo_terms": self._cell(row.get("hpo_terms", "")),
            "hpo_labels": self._cell(row.get("hpo_labels", "")),
            "phenotype_relevant_variant_count": self._cell(
                row.get("phenotype_relevant_variant_count", "")
            ),
            "data_quality": self._cell(row.get("data_quality", "")),
            "vector_score": f"{vector_score:.4f}" if vector_score > 0 else "",
        }
        return SimilarCase(
            case_id=self._cell(row.get("_id", "")),
            source=f"private-testing-case-bank:{'hybrid' if vector_score > 0 else 'lexical'}",
            summary=self._cell(row.get("case_report", ""))[:2000],
            diagnosis=self._cell(row.get("diagnosis", "")) or "无最终临床诊断；私有历史检测案例",
            score=score,
            evidence_role="testing_finding_reference",
            reported_genes=reported_genes,
            phenotype_relevant_genes=phenotype_relevant_genes,
            variant_summary=self._cell(row.get("variant_summary", ""))[:2000],
            metadata=metadata,
        )

    def _vector_recall(self, query_text: str) -> dict[tuple[str, str], float]:
        """Return vector similarity scores keyed by (bank_type, case_id)."""
        if self._vectors is None or self._vector_metadata is None or self._vectorizer is None:
            return {}
        try:
            import numpy as np

            query_vector = self._vectorizer.transform([query_text]).astype("float32")[0]
            vectors = self._vectors
            similarities = np.asarray(vectors @ query_vector, dtype="float32")
            if similarities.size == 0:
                return {}
            top_n = min(self.vector_top_n, similarities.size)
            if top_n <= 0:
                return {}
            if top_n < similarities.size:
                candidate_indices = np.argpartition(similarities, -top_n)[-top_n:]
            else:
                candidate_indices = np.arange(similarities.size)
            candidate_indices = candidate_indices[np.argsort(similarities[candidate_indices])[::-1]]
            scores: dict[tuple[str, str], float] = {}
            for idx in candidate_indices:
                score = max(0.0, float(similarities[idx]))
                if score <= 0:
                    continue
                row = self._vector_metadata.iloc[int(idx)]
                bank_type = self._cell(row.get("bank_type", ""))
                case_id = self._cell(row.get("case_id", ""))
                if bank_type and case_id:
                    scores[(bank_type, case_id)] = score
            return scores
        except Exception:
            return {}

    def _iter_recalled_rows(
        self,
        bank_type: str,
        vector_scores: dict[tuple[str, str], float],
    ) -> list[tuple[object, float]]:
        if not vector_scores:
            bank = self._public_bank if bank_type == "public" else self._private_bank
            return [(row, 0.0) for _, row in bank.iterrows()]  # type: ignore[union-attr]

        by_id = self._public_by_id if bank_type == "public" else self._private_by_id
        rows: list[tuple[object, float]] = []
        for (candidate_bank, case_id), vector_score in vector_scores.items():
            if candidate_bank != bank_type:
                continue
            try:
                row = by_id.loc[case_id]  # type: ignore[union-attr]
            except Exception:
                continue
            rows.append((row, vector_score))
        if not rows:
            bank = self._public_bank if bank_type == "public" else self._private_bank
            return [(row, 0.0) for _, row in bank.iterrows()]  # type: ignore[union-attr]
        return rows

    def _combine_scores(self, rule_score: float, vector_score: float) -> float:
        if rule_score <= 0:
            return 0.0
        if vector_score <= 0:
            return rule_score
        vector_weight = min(max(self.vector_weight, 0.0), 1.0)
        return round(rule_score * (1.0 - vector_weight) + vector_score * vector_weight, 4)

    def search(
        self,
        patient: PatientProfile,
        phenotypes: list[PhenotypeItem],
    ) -> list[SimilarCase]:
        """Search both public case reports and private historical testing cases."""
        self._ensure_loaded()
        context = self._query_context(patient, phenotypes)
        if not context["text"]:
            return []

        public_results: list[SimilarCase] = []
        private_results: list[SimilarCase] = []
        vector_scores = self._vector_recall(str(context["text"]))

        for row, vector_score in self._iter_recalled_rows("public", vector_scores):
            score = self._combine_scores(self._score_public_row(row, context), vector_score)
            if score >= self.min_score:
                public_results.append(self._public_case_from_row(row, score, vector_score))

        for row, vector_score in self._iter_recalled_rows("private", vector_scores):
            score = self._combine_scores(self._score_private_row(row, context), vector_score)
            if score >= self.min_score:
                private_results.append(self._private_case_from_row(row, score, vector_score))

        public_results.sort(key=lambda item: item.score or 0.0, reverse=True)
        private_results.sort(key=lambda item: item.score or 0.0, reverse=True)
        return public_results[: self.public_return_k] + private_results[: self.private_return_k]


@dataclass(slots=True)
class DeepRareCaseSearcher:
    """DeepRare-style local case search using the embedded case bank."""

    openai_api_key: str
    openai_base_url: str = ""
    embedding_model: str = "text-embedding-3-small"
    case_bank_path: str = "./database/RDS_embeddings.csv"
    top_n: int = 50
    return_k: int = 3
    llm_filter: bool = False
    filter_model_name: str = "gpt-4.1"
    _embedding_api: object | None = None
    _filter_api: object | None = None
    _case_bank: object | None = None

    @classmethod
    def from_environment(cls) -> "DeepRareCaseSearcher | None":
        """Build the searcher when the embedded case bank is available."""
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        case_bank_path = os.getenv(
            "YK_FERTA_CASE_BANK_PATH",
            "./database/RDS_embeddings.csv",
        )
        if not api_key or not os.path.exists(case_bank_path):
            return None
        return cls(
            openai_api_key=api_key,
            openai_base_url=os.getenv("OPENAI_BASE_URL", "").strip(),
            embedding_model=os.getenv(
                "YK_FERTA_CASE_EMBED_MODEL",
                "text-embedding-3-small",
            ),
            case_bank_path=case_bank_path,
            top_n=int(os.getenv("YK_FERTA_CASE_TOP_N", "50")),
            return_k=int(os.getenv("YK_FERTA_CASE_RETURN_K", "3")),
            llm_filter=os.getenv("YK_FERTA_CASE_LLM_FILTER", "0").strip() == "1",
            filter_model_name=os.getenv("YK_FERTA_CASE_FILTER_MODEL", "gpt-4.1"),
        )

    def _ensure_loaded(self) -> None:
        """Load the OpenAI client and case bank on first use."""
        if self._embedding_api is None:
            from api.interface import Openai_api

            self._embedding_api = Openai_api(
                self.openai_api_key,
                self.filter_model_name,
                base_url=self.openai_base_url,
            )
            if self.llm_filter:
                self._filter_api = self._embedding_api

        if self._case_bank is None:
            import pandas as pd

            case_bank = pd.read_csv(self.case_bank_path)
            required = ["_id", "case_report", "embedding", "diagnosis"]
            missing = [column for column in required if column not in case_bank.columns]
            if missing:
                raise ValueError(
                    f"Case bank {self.case_bank_path} missing required columns: {missing}"
                )
            case_bank = case_bank[required].copy()
            case_bank = case_bank[case_bank["embedding"].notna()]
            case_bank = case_bank[case_bank["diagnosis"].notna()]
            self._case_bank = case_bank.reset_index(drop=True)

    def _build_query(
        self,
        patient: PatientProfile,
        phenotypes: list[PhenotypeItem],
    ) -> str:
        labels = [item.label for item in phenotypes if item.label]
        if labels:
            return ", ".join(labels[:15])
        return patient.narrative()[:1000]

    def _parse_embedding(self, value: str) -> list[float]:
        try:
            parsed = ast.literal_eval(value)
        except Exception:
            return []
        if not isinstance(parsed, list):
            return []
        return [float(item) for item in parsed]

    def _cosine_similarity(self, left: list[float], right: list[float]) -> float:
        if not left or not right or len(left) != len(right):
            return 0.0
        numerator = sum(a * b for a, b in zip(left, right))
        left_norm = sum(a * a for a in left) ** 0.5
        right_norm = sum(b * b for b in right) ** 0.5
        if left_norm == 0.0 or right_norm == 0.0:
            return 0.0
        return numerator / (left_norm * right_norm)

    def _llm_case_match(self, query_text: str, case_text: str) -> bool:
        if not self.llm_filter or self._filter_api is None:
            return True
        from tools.llm_agent import Check_Patient_Agent

        try:
            return bool(Check_Patient_Agent(query_text, case_text, self._filter_api))
        except Exception:
            return True

    def _tokenize(self, text: str) -> set[str]:
        return {token for token in re.findall(r"[a-z0-9]+", text.lower()) if len(token) > 1}

    def _lexical_similarity(self, query_text: str, case_text: str) -> float:
        query_tokens = self._tokenize(query_text)
        case_tokens = self._tokenize(case_text)
        if not query_tokens or not case_tokens:
            return 0.0
        overlap = len(query_tokens & case_tokens)
        return overlap / len(query_tokens)

    def search(
        self,
        patient: PatientProfile,
        phenotypes: list[PhenotypeItem],
    ) -> list[SimilarCase]:
        """Retrieve similar cases from the local embedded case bank."""
        query_text = self._build_query(patient, phenotypes)
        if not query_text:
            return []

        self._ensure_loaded()

        working = self._case_bank.copy()
        similarity_mode = "embedding"
        try:
            query_embedding = self._embedding_api.get_embedding(
                query_text,
                model=self.embedding_model,
            )
            working["similarity"] = working["embedding"].apply(
                lambda value: self._cosine_similarity(
                    self._parse_embedding(value),
                    query_embedding,
                )
            )
        except Exception:
            similarity_mode = "lexical-fallback"
            working["similarity"] = working["case_report"].apply(
                lambda value: self._lexical_similarity(query_text, str(value))
            )

        head = working.sort_values("similarity", ascending=False).head(self.top_n)

        similar_cases: list[SimilarCase] = []
        for _, row in head.iterrows():
            if len(similar_cases) >= self.return_k:
                break
            if not self._llm_case_match(query_text, str(row["case_report"])):
                continue
            similar_cases.append(
                SimilarCase(
                    case_id=str(row["_id"]),
                    source=f"local-case-bank:{similarity_mode}",
                    summary=str(row["case_report"])[:2000],
                    diagnosis=str(row["diagnosis"]),
                    score=float(row["similarity"]),
                )
            )

        return similar_cases


@dataclass(slots=True)
class LlmInitialDiagnosisSynthesizer:
    """Two-pass LLM synthesis that mirrors DeepRare's first-round diagnosis stage."""

    api_key: str
    model_name: str
    base_url: str = ""
    _reasoner: _OpenAIReasoner | None = None

    def _ensure_reasoner(self) -> None:
        if self._reasoner is None:
            self._reasoner = _OpenAIReasoner(self.api_key, self.model_name, self.base_url)

    @staticmethod
    def _format_initial_similar_case(
        case: SimilarCase,
        *,
        has_patient_molecular_evidence: bool,
    ) -> str:
        if case.evidence_role == "testing_finding_reference" and not has_patient_molecular_evidence:
            phenotype_context = case.metadata.get("hpo_labels", "") or case.metadata.get("retrieval_tags", "")
            return (
                f"- role={case.evidence_role}; source={case.source}; "
                f"note=私有历史检测参考，无最终临床诊断；score={case.score}; "
                f"phenotype_context={phenotype_context or '-'}"
            )

        gene_text = ",".join(case.phenotype_relevant_genes or case.reported_genes[:5])
        return (
            f"- role={case.evidence_role}; source={case.source}; "
            f"diagnosis_or_label={case.diagnosis}; score={case.score}; "
            f"genes={gene_text}; summary={_truncate(case.summary, 300)}"
        )

    def _heuristic_candidates(
        self,
        phenotypes: list[PhenotypeItem],
        phenotype_hints: list[PhenotypeToolHit],
        similar_cases: list[SimilarCase],
        top_k: int,
        has_patient_molecular_evidence: bool = True,
    ) -> list[CandidateCondition]:
        ranked_names: list[str] = []
        for hit in phenotype_hints:
            ranked_names.append(hit.disease_name)
        for case in similar_cases:
            if case.evidence_role == "diagnosis_reference":
                ranked_names.append(case.diagnosis)

        candidates: list[CandidateCondition] = []
        seen: set[str] = set()
        for rank, name in enumerate(ranked_names, start=1):
            clean_name, rationale = _soften_unconfirmed_molecular_candidate(
                _normalize_text(name),
                "根据表型工具或本地相似病例得到的启发式候选。",
                has_patient_molecular_evidence,
            )
            key = _normalize_key(clean_name)
            if not key or key in seen:
                continue
            seen.add(key)
            candidates.append(
                CandidateCondition(
                    name=clean_name,
                    rank=len(candidates) + 1,
                    score=max(0.1, 0.9 - rank * 0.1),
                    rationale=rationale,
                    supporting_phenotypes=[item.label for item in phenotypes[:5]],
                )
            )
            if len(candidates) >= top_k:
                break
        return candidates

    def _parse_candidates(
        self,
        payload: object,
        top_k: int,
        phenotypes: list[PhenotypeItem],
        has_patient_molecular_evidence: bool = True,
    ) -> list[CandidateCondition]:
        if not isinstance(payload, dict):
            return []
        raw_candidates = payload.get("candidates", [])
        if not isinstance(raw_candidates, list):
            return []

        candidates: list[CandidateCondition] = []
        seen: set[str] = set()
        for item in raw_candidates:
            if not isinstance(item, dict):
                continue
            name = _normalize_text(str(item.get("name", "")))
            rationale = _truncate(str(item.get("rationale", "")), 1000)
            name, rationale = _soften_unconfirmed_molecular_candidate(
                name,
                rationale,
                has_patient_molecular_evidence,
            )
            key = _normalize_key(name)
            if not key or key in seen:
                continue
            seen.add(key)
            support = item.get("supporting_phenotypes", [])
            if not isinstance(support, list):
                support = []
            candidates.append(
                CandidateCondition(
                    name=name,
                    rank=len(candidates) + 1,
                    score=_safe_float(item.get("score"), default=max(0.1, 0.9 - len(candidates) * 0.1)),
                    rationale=rationale,
                    supporting_phenotypes=[
                        _normalize_text(str(label))
                        for label in support[:8]
                        if _normalize_text(str(label))
                    ] or [item.label for item in phenotypes[:5]],
                )
            )
            if len(candidates) >= top_k:
                break
        return candidates

    def synthesize(
        self,
        patient: PatientProfile,
        phenotypes: list[PhenotypeItem],
        phenotype_hints: list[PhenotypeToolHit],
        knowledge_evidence: list[EvidenceItem],
        similar_cases: list[SimilarCase],
        top_k: int,
    ) -> list[CandidateCondition]:
        has_patient_molecular_evidence = _patient_has_molecular_evidence(patient)
        molecular_policy = _molecular_evidence_policy(has_patient_molecular_evidence)
        heuristic = self._heuristic_candidates(
            phenotypes,
            phenotype_hints,
            similar_cases,
            top_k,
            has_patient_molecular_evidence,
        )
        if not self.api_key:
            return heuristic

        self._ensure_reasoner()

        phenotype_block = "\n".join(
            f"- {item.label} ({item.code or 'no-code'})"
            for item in phenotypes[:12]
        )
        hint_block = "\n".join(
            f"- {hit.source}: {hit.disease_name}"
            for hit in phenotype_hints[:10]
        )
        similar_case_block = "\n".join(
            self._format_initial_similar_case(
                case,
                has_patient_molecular_evidence=has_patient_molecular_evidence,
            )
            for case in similar_cases[:5]
        )
        evidence_block = "\n".join(
            f"- {item.source_type}: {item.title}; { _truncate(item.summary, 300) }"
            for item in knowledge_evidence[:6]
        )
        initial_stage_policy = (
            "本阶段目标：生成第一轮鉴别诊断候选，而不是最终诊断结论。\n"
            "请优先给出能够被当前病例表型直接支持的临床疾病/综合征名称，并按可能性排序。\n"
            "候选命名粒度要求：\n"
            "1. 优先使用临床疾病/综合征层名称。\n"
            "2. 不要使用过于宽泛的上位概念，如 reproductive disorder、reproductive wastage、female infertility disorder、pregnancy disorder，除非没有更具体的临床疾病名称可用。\n"
            "3. 不要把病因机制、基因相关命名、检测发现或病理解释当作候选主名称。\n"
            "4. 如果存在疾病层名称与更具体的分子/机制层名称，优先输出疾病层名称，并把分子机制写入 rationale。\n"
            "5. phenotype_tool_hint 只是候选提示，不是诊断结论；private testing reference 只是检测经验参考，不是确诊病例证据。"
        )

        case_only_prompt = (
            "只返回 JSON，不要输出 Markdown 或额外解释。JSON 结构为 "
            "{\"candidates\":[{\"name\":\"...\",\"score\":0.0,\"rationale\":\"...\","
            "\"supporting_phenotypes\":[\"...\"]}]}. "
            f"最多给出 {top_k} 个候选疾病/综合征。\n\n"
            "输出语言要求：除疾病英文名、HPO、OMIM、PubMed 标题、基因名等必要英文术语外，"
            "rationale 和 supporting_phenotypes 必须以中文为主。\n\n"
            f"{initial_stage_policy}\n\n"
            "候选命名规则：\n"
            f"{molecular_policy}\n\n"
            "如果当前病例没有患者本人的分子检测结果，候选主名称应优先使用临床综合征/疾病名称。"
            "不要把“GENE-related / 某基因相关 / 某基因突变导致”作为候选主名称；"
            "基因只能作为可能的分子机制写在理由中。\n\n"
            f"病例信息：\n{patient.narrative()}"
        )
        case_only = self._reasoner.complete(
            "你是面向中文医生用户的临床推理助手。仅基于当前病例信息生成第一轮鉴别诊断候选。目标是稳定地产出临床疾病/综合征层候选，并严格区分临床诊断和未确认的分子病因。",
            case_only_prompt,
        )

        evidence_prompt = (
            "只返回 JSON，不要输出 Markdown 或额外解释。JSON 结构为 "
            "{\"candidates\":[{\"name\":\"...\",\"score\":0.0,\"rationale\":\"...\","
            "\"supporting_phenotypes\":[\"...\"]}]}. "
            f"最多给出 {top_k} 个候选疾病/综合征。\n\n"
            "输出语言要求：除疾病英文名、HPO、OMIM、PubMed 标题、基因名等必要英文术语外，"
            "rationale 和 supporting_phenotypes 必须以中文为主。\n\n"
            f"{initial_stage_policy}\n\n"
            "候选命名规则：\n"
            f"{molecular_policy}\n\n"
            "如果当前病例没有患者本人的分子检测结果，候选主名称应优先使用临床综合征/疾病名称。"
            "不要把“GENE-related / 某基因相关 / 某基因突变导致”作为候选主名称；"
            "基因只能作为可能的分子机制写在理由中。\n\n"
            f"病例信息：\n{patient.narrative()}\n\n"
            f"已确认表型：\n{phenotype_block}\n\n"
            f"表型工具候选提示：\n{hint_block or '- 无'}\n\n"
            f"本地相似病例：\n{similar_case_block or '- 无'}\n\n"
            "重要约束：role=testing_finding_reference 的案例是私有历史检测案例，"
            "不是最终临床诊断。只能作为表型/检测策略经验参考，不能当作确诊病例证据，"
            "也不能主导候选病名的基因化命名。\n\n"
            f"外部知识证据摘要：\n{evidence_block or '- 无'}\n\n"
            f"仅基于病例的第一轮判断：\n{case_only or '无'}"
        )
        merged = self._reasoner.complete(
            "你是罕见病/不孕不育诊断辅助系统的初诊综合模块。请整合表型、相似病例和检索知识，输出排序后的第一轮鉴别诊断候选。优先稳定地产出临床疾病/综合征层候选，不要输出过宽泛的上位概念，也不要把未确认的基因假设写成候选主名称或已确认分子诊断。",
            evidence_prompt,
        )

        parsed = self._parse_candidates(
            _safe_json_loads(merged or ""),
            top_k,
            phenotypes,
            has_patient_molecular_evidence,
        )
        if parsed:
            return parsed

        parsed_case_only = self._parse_candidates(
            _safe_json_loads(case_only or ""),
            top_k,
            phenotypes,
            has_patient_molecular_evidence,
        )
        if parsed_case_only:
            return parsed_case_only

        return heuristic


@dataclass(slots=True)
class LocalDiseaseNormalizer:
    """Normalize disease names via top-N recall plus constrained LLM adjudication."""

    _shared_embedding_cache: ClassVar[dict[str, object]] = {}
    _shared_tokenizer_cache: ClassVar[dict[str, object]] = {}
    _shared_encoder_cache: ClassVar[dict[str, object]] = {}

    api_key: str = ""
    model_name: str = "gpt-4.1"
    base_url: str = ""
    orpha_concept2id_path: str = "./database/orpha_concept2id.json"
    orpha2name_path: str = "./database/orpha2name.json"
    orpha2omim_path: str = "./database/orpha2omim.json"
    concept_embedding_path: str = "./database/embeds_concept.pt"
    concept_encoder_model: str = "FremyCompany/BioLORD-2023-C"
    top_n: int = 5
    llm_temperature: float = 0.0
    _concept2id: dict[str, str] | None = None
    _orpha2name: dict[str, str] | None = None
    _orpha2omim: dict[str, str] | None = None
    _concept_names: list[str] | None = None
    _concept_ids: list[str] | None = None
    _concept_embeddings: object | None = None
    _tokenizer: object | None = None
    _encoder: object | None = None
    _reasoner: _OpenAIReasoner | None = None

    def _ensure_loaded(self) -> None:
        if self._concept2id is not None:
            return
        with open(self.orpha_concept2id_path, "r", encoding="utf-8-sig") as handle:
            self._concept2id = json.load(handle)
        with open(self.orpha2name_path, "r", encoding="utf-8-sig") as handle:
            self._orpha2name = json.load(handle)
        with open(self.orpha2omim_path, "r", encoding="utf-8-sig") as handle:
            self._orpha2omim = json.load(handle)
        self._concept_names = list(self._concept2id.keys())
        self._concept_ids = list(self._concept2id.values())

    def _ensure_embeddings(self) -> None:
        self._ensure_loaded()
        if self._concept_embeddings is not None:
            return
        import torch

        cache_key = str(Path(self.concept_embedding_path).resolve())
        cached = self._shared_embedding_cache.get(cache_key)
        if cached is None:
            loaded = torch.load(Path(self.concept_embedding_path), map_location="cpu", weights_only=False)
            embeddings = torch.as_tensor(loaded, dtype=torch.float32)
            if embeddings.ndim != 2:
                raise ValueError(f"Invalid concept embedding shape: {tuple(embeddings.shape)}")
            cached = torch.nn.functional.normalize(embeddings, dim=1)
            self._shared_embedding_cache[cache_key] = cached
        self._concept_embeddings = cached

    def _ensure_encoder(self) -> None:
        if self._tokenizer is not None and self._encoder is not None:
            return
        from transformers import AutoModel, AutoTokenizer

        model_key = self.concept_encoder_model
        tokenizer = self._shared_tokenizer_cache.get(model_key)
        encoder = self._shared_encoder_cache.get(model_key)
        if tokenizer is None or encoder is None:
            tokenizer = AutoTokenizer.from_pretrained(model_key)
            encoder = AutoModel.from_pretrained(model_key)
            encoder.eval()
            self._shared_tokenizer_cache[model_key] = tokenizer
            self._shared_encoder_cache[model_key] = encoder
        self._tokenizer = tokenizer
        self._encoder = encoder

    def _ensure_reasoner(self) -> None:
        if not self.api_key or self._reasoner is not None:
            return
        self._reasoner = _OpenAIReasoner(self.api_key, self.model_name, self.base_url)

    def _candidate_queries(self, disease_name: str) -> list[str]:
        text = _normalize_text(disease_name)
        if not text:
            return []
        variants = [text]
        stripped = re.sub(r"[（(]分子病因未确认[)）]", "", text, flags=re.IGNORECASE).strip()
        stripped = re.sub(r"[（(]molecular etiology unconfirmed[)）]", "", stripped, flags=re.IGNORECASE).strip()
        if stripped and stripped not in variants:
            variants.append(stripped)
        if "(" in stripped and ")" in stripped:
            outer = stripped.split("(", 1)[0].strip()
            inner = stripped.split("(", 1)[1].split(")", 1)[0].strip()
            for item in [outer, inner]:
                if item and item not in variants:
                    variants.append(item)
        if "（" in stripped and "）" in stripped:
            outer = stripped.split("（", 1)[0].strip()
            inner = stripped.split("（", 1)[1].split("）", 1)[0].strip()
            for item in [outer, inner]:
                if item and item not in variants:
                    variants.append(item)
        return variants

    def _encode_queries(self, queries: list[str]) -> object:
        self._ensure_encoder()
        import torch

        assert self._tokenizer is not None
        assert self._encoder is not None
        encoded = self._tokenizer(
            queries,
            truncation=True,
            padding=True,
            return_tensors="pt",
            max_length=36,
        )
        with torch.no_grad():
            outputs = self._encoder(**encoded)
        embeddings = outputs.last_hidden_state[:, 0, :]
        return torch.nn.functional.normalize(embeddings, dim=1)

    def _lookup_top_matches(self, disease_name: str) -> list[tuple[str, float]]:
        self._ensure_loaded()
        self._ensure_embeddings()
        queries = self._candidate_queries(disease_name)
        if not queries:
            return []

        import torch

        assert self._concept_ids is not None
        assert self._concept_embeddings is not None
        query_embeddings = self._encode_queries(queries)
        similarities = torch.matmul(query_embeddings, self._concept_embeddings.T)
        row_count, col_count = similarities.shape
        top_k = min(max(self.top_n, 1), col_count)
        flat_scores = similarities.reshape(-1)
        top_values, top_indices = torch.topk(flat_scores, k=top_k)
        results: list[tuple[str, float]] = []
        seen_ids: set[str] = set()
        for value, flat_idx in zip(top_values.tolist(), top_indices.tolist()):
            concept_idx = int(flat_idx) % col_count
            concept_id = self._concept_ids[concept_idx]
            if concept_id in seen_ids:
                continue
            seen_ids.add(concept_id)
            results.append((concept_id, float(value)))
        return results

    def _build_match_records(
        self,
        disease_name: str,
        matches: list[tuple[str, float]],
    ) -> list[dict[str, object]]:
        assert self._orpha2name is not None
        assert self._orpha2omim is not None
        records: list[dict[str, object]] = []
        for index, (orpha_id, score) in enumerate(matches, start=1):
            records.append(
                {
                    "rank": index,
                    "original_name": disease_name,
                    "disease_id": orpha_id,
                    "normalized_name": self._orpha2name.get(orpha_id, disease_name),
                    "ontology": (
                        f"Orphanet/OMIM:{self._orpha2omim[orpha_id]}"
                        if self._orpha2omim.get(orpha_id)
                        else "Orphanet"
                    ),
                    "mapping_score": score,
                }
            )
        return records

    def _adjudicate_top_matches(
        self,
        disease_name: str,
        matches: list[dict[str, object]],
    ) -> tuple[dict[str, object] | None, str, str, float | None]:
        if not matches:
            return None, "no_match", "No recalled candidates.", 0.0
        self._ensure_reasoner()
        if self._reasoner is None:
            return matches[0], "embedding_top1_fallback", "LLM adjudication unavailable.", None

        candidate_lines = []
        for item in matches:
            candidate_lines.append(
                f"{item['rank']}. {item['normalized_name']} | {item['disease_id']} | "
                f"{item['ontology']} | similarity={float(item['mapping_score']):.4f}"
            )
        prompt = (
            "只返回 JSON，不要输出 Markdown 或额外解释。JSON 结构必须为 "
            "{\"decision\":\"select_candidate\"|\"unmapped\","
            "\"selected_rank\":1,\"confidence\":0.0,\"reason\":\"...\"}。\n\n"
            "任务：根据原始候选病名，在给定的标准疾病候选列表中选择最匹配的一个；"
            "如果前列候选都不可靠，则返回 unmapped。\n"
            "约束：\n"
            "1. 只能在给定候选中选择，不能生成新病名。\n"
            "2. 如果候选改变了核心疾病语义、病理类型或疾病系统，应返回 unmapped。\n"
            "3. 对 hydatidiform mole / trophoblastic / infertility / imprinting 这类核心词要敏感。\n"
            "4. 输出应稳定保守，不能为了凑标准化而强行选择。\n\n"
            f"原始候选病名：{disease_name}\n\n"
            "标准化候选：\n"
            + "\n".join(candidate_lines)
        )
        raw = self._reasoner.complete(
            "你正在执行疾病名标准化判别。目标是稳定、保守地从候选列表中选择最匹配的标准疾病，或拒识。",
            prompt,
            temperature=self.llm_temperature,
            seed=42,
        )
        parsed = _safe_json_loads(raw or "")
        if not isinstance(parsed, dict):
            return matches[0], "embedding_top1_fallback", "LLM returned invalid JSON.", None
        decision = _normalize_text(str(parsed.get("decision", ""))).lower()
        reason = _truncate(str(parsed.get("reason", "")), 500)
        confidence = _safe_float(parsed.get("confidence"), 0.0)
        if decision == "unmapped":
            return None, "llm_unmapped", reason or "LLM rejected recalled candidates.", confidence
        if decision != "select_candidate":
            return matches[0], "embedding_top1_fallback", reason or "LLM decision invalid.", confidence
        selected_rank = int(_safe_float(parsed.get("selected_rank"), 0))
        selected = next((item for item in matches if int(item["rank"]) == selected_rank), None)
        if selected is None:
            return matches[0], "embedding_top1_fallback", reason or "LLM selected out-of-range rank.", confidence
        return selected, "llm_topn_adjudication", reason, confidence

    def normalize(
        self,
        candidates: list[CandidateCondition],
    ) -> list[NormalizedDisease]:
        self._ensure_loaded()
        assert self._orpha2name is not None
        assert self._orpha2omim is not None

        normalized: list[NormalizedDisease] = []
        for candidate in candidates:
            try:
                recalled_matches = self._lookup_top_matches(candidate.name)
            except Exception:
                recalled_matches = []
            match_records = self._build_match_records(candidate.name, recalled_matches)
            selected, decision_source, decision_reason, decision_confidence = self._adjudicate_top_matches(
                candidate.name,
                match_records,
            )
            if not selected:
                normalized.append(
                    NormalizedDisease(
                        original_name=candidate.name,
                        normalized_name=candidate.name,
                        ontology="unmapped",
                        mapping_score=match_records[0]["mapping_score"] if match_records else 0.0,
                        normalization_top_matches=match_records,
                        normalization_decision_source=decision_source,
                        normalization_decision_reason=decision_reason,
                        normalization_decision_confidence=decision_confidence,
                    )
                )
                continue
            normalized.append(
                NormalizedDisease(
                    original_name=candidate.name,
                    normalized_name=str(selected["normalized_name"]),
                    disease_id=str(selected["disease_id"]),
                    ontology=str(selected["ontology"]),
                    mapping_score=_safe_float(selected["mapping_score"]),
                    normalization_top_matches=match_records,
                    normalization_decision_source=decision_source,
                    normalization_decision_reason=decision_reason,
                    normalization_decision_confidence=decision_confidence,
                )
            )
        return normalized


@dataclass(slots=True)
class LlmPerDiseaseVerifier:
    """Per-candidate review using local disease knowledge and accumulated evidence."""

    api_key: str
    model_name: str
    base_url: str = ""
    orphanet_path: str = "./database/orpha_disorders_HP_map.json"
    candidate_pubmed_results: int = 2
    _orphanet: dict[str, dict] | None = None
    _reasoner: _OpenAIReasoner | None = None
    last_candidate_evidence: list[EvidenceItem] = field(default_factory=list)

    def _ensure_loaded(self) -> None:
        if self._orphanet is None:
            with open(self.orphanet_path, "r", encoding="utf-8-sig") as handle:
                self._orphanet = json.load(handle)
        if self._reasoner is None and self.api_key:
            self._reasoner = _OpenAIReasoner(self.api_key, self.model_name, self.base_url)

    def _phenotype_overlap(
        self,
        phenotypes: list[PhenotypeItem],
        disease_entry: dict | None,
    ) -> int:
        if not disease_entry:
            return 0
        patient_terms = {_normalize_key(item.label) for item in phenotypes if item.label}
        overlap = 0
        for association in disease_entry.get("hpo_associations", [])[:50]:
            if not association:
                continue
            label = _normalize_key(str(association[0]))
            if label and label in patient_terms:
                overlap += 1
        return overlap

    @staticmethod
    def _candidate_key(candidate: NormalizedDisease) -> str:
        raw = candidate.disease_id or candidate.normalized_name or candidate.original_name
        key = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")
        return key[:80] or "candidate"

    @staticmethod
    def _omim_ids(candidate: NormalizedDisease) -> list[str]:
        text = " ".join(
            str(value)
            for value in [
                candidate.disease_id,
                candidate.ontology,
                candidate.original_name,
                candidate.normalized_name,
            ]
            if value
        )
        return list(dict.fromkeys(re.findall(r"OMIM:?(\d{3,})", text, flags=re.IGNORECASE)))

    def _orphanet_evidence(
        self,
        candidate: NormalizedDisease,
        disease_entry: dict | None,
    ) -> list[EvidenceItem]:
        if not disease_entry:
            return []
        hpo_lines = []
        for association in disease_entry.get("hpo_associations", [])[:20]:
            if not association:
                continue
            label = str(association[0]) if len(association) > 0 else ""
            code = str(association[1]) if len(association) > 1 else ""
            frequency = str(association[2]) if len(association) > 2 else ""
            hpo_lines.append(" ".join(part for part in [label, code, frequency] if part))
        summary = (
            f"Orphanet 本地疾病知识：{disease_entry.get('name', candidate.normalized_name)}。"
            f"关联 HPO：{'; '.join(hpo_lines) if hpo_lines else '未列出'}"
        )
        return [
            EvidenceItem(
                source_id=f"candidate-orphanet-{self._candidate_key(candidate)}",
                source_type="orphanet",
                title=f"Orphanet: {disease_entry.get('name', candidate.normalized_name)}",
                summary=summary,
                url=str(disease_entry.get("expert_link", "")),
                metadata={
                    "role": "candidate_orphanet_evidence",
                    "candidate": candidate.normalized_name,
                    "disease_id": candidate.disease_id or "",
                },
            )
        ]

    def _omim_evidence(self, candidate: NormalizedDisease) -> list[EvidenceItem]:
        evidence: list[EvidenceItem] = []
        for omim_id in self._omim_ids(candidate)[:2]:
            try:
                from tools.omim_search import OMIMSearchTool

                summary = OMIMSearchTool(f"OMIM:{omim_id}")
            except Exception as exc:
                summary = f"OMIM 检索失败：{exc}"
            if not DeepRareKnowledgeSearcher._is_useful_summary(summary):
                continue
            evidence.append(
                EvidenceItem(
                    source_id=f"candidate-omim-{omim_id}",
                    source_type="omim",
                    title=f"OMIM:{omim_id} {candidate.normalized_name}",
                    summary=summary[:2500],
                    url=f"https://www.omim.org/entry/{omim_id}",
                    metadata={
                        "role": "candidate_omim_evidence",
                        "candidate": candidate.normalized_name,
                        "omim_id": omim_id,
                    },
                )
            )
        return evidence

    @staticmethod
    def _candidate_pubmed_query(candidate: NormalizedDisease) -> str:
        names = [candidate.normalized_name, candidate.original_name]
        names = [name for name in names if name]
        unique_names = list(dict.fromkeys(names))
        quoted = [f'"{name}"[Title/Abstract]' for name in unique_names[:2]]
        query = " OR ".join(quoted)
        if re.search(r"hydatidiform|mole|葡萄胎", " ".join(unique_names), re.IGNORECASE):
            query = (
                f"({query}) AND (NLRP7 OR KHDC3L OR infertility OR "
                '"recurrent pregnancy loss" OR "molar pregnancy")'
            )
        elif re.search(r"infertility|fertility|不孕", " ".join(unique_names), re.IGNORECASE):
            query = f"({query}) AND (infertility OR fertility OR reproductive)"
        return query or candidate.normalized_name or candidate.original_name

    def _pubmed_evidence(self, candidate: NormalizedDisease) -> list[EvidenceItem]:
        query = self._candidate_pubmed_query(candidate)
        if not query:
            return []
        try:
            searcher = DeepRareKnowledgeSearcher(pubmed_results=self.candidate_pubmed_results)
            items = searcher._search_pubmed_direct(query, self.candidate_pubmed_results)
        except Exception:
            return []
        candidate_key = self._candidate_key(candidate)
        evidence: list[EvidenceItem] = []
        for item in items:
            metadata = dict(item.metadata)
            metadata.update(
                {
                    "role": "candidate_pubmed_evidence",
                    "candidate": candidate.normalized_name,
                    "candidate_query": query,
                }
            )
            evidence.append(
                EvidenceItem(
                    source_id=f"candidate-{candidate_key}-{item.source_id}",
                    source_type=item.source_type,
                    title=item.title,
                    summary=item.summary,
                    citation=item.citation,
                    url=item.url,
                    relevance_score=item.relevance_score,
                    metadata=metadata,
                )
            )
        return evidence

    def _collect_candidate_evidence(
        self,
        candidate: NormalizedDisease,
        disease_entry: dict | None,
    ) -> list[EvidenceItem]:
        evidence: list[EvidenceItem] = []
        evidence.extend(self._orphanet_evidence(candidate, disease_entry))
        evidence.extend(self._omim_evidence(candidate))
        evidence.extend(self._pubmed_evidence(candidate))

        seen: set[str] = set()
        deduped: list[EvidenceItem] = []
        for item in evidence:
            if item.source_id in seen:
                continue
            seen.add(item.source_id)
            deduped.append(item)
        return deduped

    @staticmethod
    def _review_needs_chinese_localization(review: CandidateReview) -> bool:
        values = [review.reasoning]
        values.extend(review.supporting_evidence or [])
        values.extend(review.contradicting_evidence or [])
        values.extend(review.missing_evidence or [])
        return any(_contains_latin_text(_normalize_text(str(item))) for item in values if item)

    def _localize_reviews_for_chinese_display(
        self,
        reviews: list[CandidateReview],
    ) -> list[CandidateReview]:
        pending = [
            (index, review)
            for index, review in enumerate(reviews)
            if self._review_needs_chinese_localization(review)
        ]
        if not pending or not self.api_key:
            return reviews

        self._ensure_loaded()
        if self._reasoner is None:
            return reviews

        payload = []
        for index, review in pending:
            payload.append(
                {
                    "index": index,
                    "candidate_name": review.candidate_name,
                    "reasoning": review.reasoning,
                    "supporting_evidence": list(review.supporting_evidence or []),
                    "contradicting_evidence": list(review.contradicting_evidence or []),
                    "missing_evidence": list(review.missing_evidence or []),
                }
            )

        prompt = (
            "只返回 JSON，不要输出 Markdown 或额外解释。JSON 结构为 "
            "{\"reviews\":[{\"index\":0,\"reasoning\":\"中文说明\",\"supporting_evidence\":[\"...\"],"
            "\"contradicting_evidence\":[\"...\"],\"missing_evidence\":[\"...\"]}]}。\n\n"
            "任务：把候选疾病复核结果整理成适合中文医生阅读的展示形式。\n"
            "规则：\n"
            "1. reasoning、supporting_evidence、contradicting_evidence、missing_evidence 必须以中文为主。\n"
            "2. HPO、OMIM、基因名、文献标题、综合征英文缩写可保留英文，但不要直接输出整句英文。\n"
            "3. 不新增事实，不改变支持/反对/缺失的原意，只做展示语言整理。\n"
            "4. 列表条目尽量简洁，适合前端直接展示。\n\n"
            f"待处理复核结果：\n{json.dumps(payload, ensure_ascii=False)}"
        )
        raw = self._reasoner.complete(
            "你正在为中文临床产品整理候选疾病复核结果。目标是保留原始医学含义，同时把展示文本统一成中文。",
            prompt,
            temperature=0.0,
            seed=42,
        )
        parsed = _safe_json_loads(raw or "")
        if not isinstance(parsed, dict):
            return reviews
        localized_items = parsed.get("reviews", [])
        if not isinstance(localized_items, list):
            return reviews

        localized_by_index: dict[int, dict[str, object]] = {}
        for item in localized_items:
            if not isinstance(item, dict):
                continue
            try:
                index = int(item.get("index"))
            except Exception:
                continue
            localized_by_index[index] = item

        localized_reviews: list[CandidateReview] = []
        for index, review in enumerate(reviews):
            item = localized_by_index.get(index)
            if not item:
                localized_reviews.append(review)
                continue
            localized_reviews.append(
                CandidateReview(
                    candidate_name=review.candidate_name,
                    is_supported=review.is_supported,
                    confidence=review.confidence,
                    reasoning=_truncate(
                        _normalize_text(str(item.get("reasoning", ""))) or review.reasoning,
                        1200,
                    ),
                    evidence_ids=list(review.evidence_ids or []),
                    supporting_evidence=[
                        _normalize_text(str(v))
                        for v in item.get("supporting_evidence", [])[:8]
                        if _normalize_text(str(v))
                    ]
                    or list(review.supporting_evidence or []),
                    contradicting_evidence=[
                        _normalize_text(str(v))
                        for v in item.get("contradicting_evidence", [])[:8]
                        if _normalize_text(str(v))
                    ]
                    or list(review.contradicting_evidence or []),
                    missing_evidence=[
                        _normalize_text(str(v))
                        for v in item.get("missing_evidence", [])[:8]
                        if _normalize_text(str(v))
                    ]
                    or list(review.missing_evidence or []),
                )
            )
        return localized_reviews

    def verify(
        self,
        patient: PatientProfile,
        phenotypes: list[PhenotypeItem],
        similar_cases: list[SimilarCase],
        knowledge_evidence: list[EvidenceItem],
        normalized_candidates: list[NormalizedDisease],
    ) -> list[CandidateReview]:
        self._ensure_loaded()
        assert self._orphanet is not None

        reviews: list[CandidateReview] = []
        self.last_candidate_evidence = []
        has_patient_molecular_evidence = _patient_has_molecular_evidence(patient)
        molecular_policy = _molecular_evidence_policy(has_patient_molecular_evidence)
        phenotype_block = "\n".join(f"- {item.label}" for item in phenotypes[:12])

        for candidate in normalized_candidates:
            disease_entry = self._orphanet.get(candidate.disease_id or "")
            candidate_evidence = self._collect_candidate_evidence(candidate, disease_entry)
            self.last_candidate_evidence.extend(candidate_evidence)
            overlap = self._phenotype_overlap(phenotypes, disease_entry)
            diagnosis_reference_cases = [
                case
                for case in similar_cases
                if case.evidence_role in {"", "diagnosis_reference"}
            ]
            testing_reference_cases = [
                case
                for case in similar_cases
                if case.evidence_role == "testing_finding_reference"
            ]
            support_case_count = sum(
                1
                for case in diagnosis_reference_cases
                if _normalize_key(candidate.normalized_name) in _normalize_key(case.diagnosis)
            )
            similar_case_block = "\n".join(
                f"- role={case.evidence_role}; source={case.source}; "
                f"diagnosis_or_label={case.diagnosis}: {_truncate(case.summary, 250)}"
                for case in (diagnosis_reference_cases[:3] + testing_reference_cases[:2])
            )
            disease_block = ""
            if disease_entry:
                disease_block = (
                    f"Disease name: {disease_entry.get('name', candidate.normalized_name)}\n"
                    f"Expert link: {disease_entry.get('expert_link', '')}\n"
                    "Associated HPO:\n"
                    + "\n".join(
                        f"- {assoc[0]} ({assoc[1]})"
                        for assoc in disease_entry.get("hpo_associations", [])[:10]
                    )
                )
            candidate_evidence_block = "\n".join(
                f"- {item.source_id}: {item.title}; {_truncate(item.summary, 250)}"
                for item in (candidate_evidence + knowledge_evidence)[:8]
            )

            heuristic_supported = overlap > 0 or support_case_count > 0
            heuristic_confidence = min(0.95, 0.2 + 0.15 * overlap + 0.2 * support_case_count)
            molecular_assertion_without_evidence = (
                not has_patient_molecular_evidence
                and (
                    _contains_molecular_assertion(candidate.original_name)
                    or _contains_molecular_assertion(candidate.normalized_name)
                )
            )
            if molecular_assertion_without_evidence:
                heuristic_confidence = min(heuristic_confidence, 0.65)

            if self._reasoner is None:
                reviews.append(
                    CandidateReview(
                        candidate_name=candidate.normalized_name,
                        is_supported=heuristic_supported,
                        confidence=heuristic_confidence,
                        reasoning=(
                            f"启发式复核：疾病知识中的表型重叠数={overlap}，"
                            f"支持性的公共相似病例数={support_case_count}。"
                            + (
                                _UNCONFIRMED_MOLECULAR_CLAIM_NOTE
                                if molecular_assertion_without_evidence
                                else ""
                            )
                        ),
                        evidence_ids=[item.source_id for item in (candidate_evidence + knowledge_evidence)[:6]],
                        supporting_evidence=[
                            item.source_id for item in candidate_evidence[:4]
                        ],
                        missing_evidence=[
                            _UNCONFIRMED_MOLECULAR_CLAIM_NOTE
                        ]
                        if molecular_assertion_without_evidence
                        else [],
                    )
                )
                continue

            prompt = (
                "只返回 JSON，不要输出 Markdown 或额外解释。JSON 结构为 "
                "{\"is_supported\": true, \"confidence\": 0.0, "
                "\"reasoning\": \"...\", \"evidence_ids\": [\"...\"], "
                "\"supporting_evidence\": [\"...\"], \"contradicting_evidence\": [\"...\"], "
                "\"missing_evidence\": [\"...\"]}.\n\n"
                "输出语言要求：reasoning、supporting_evidence、contradicting_evidence、missing_evidence "
                "必须以中文为主；疾病英文名、HPO、OMIM、基因名、文献标题可保留英文，但不要直接输出整句英文。\n\n"
                f"候选疾病：{candidate.normalized_name}\n"
                f"标准化 ID：{candidate.disease_id or '无'} / {candidate.ontology}\n\n"
                f"病例信息：\n{patient.narrative()}\n\n"
                f"患者表型：\n{phenotype_block}\n\n"
                f"分子证据规则：\n{molecular_policy}\n\n"
                f"本地疾病知识：\n{disease_block or '无'}\n\n"
                f"相似病例：\n{similar_case_block or '- 无'}\n\n"
                "重要约束：role=testing_finding_reference 表示没有最终临床诊断的私有历史检测案例。"
                "它可以支持表型/基因检测相关性，但不能计为确诊诊断匹配。\n\n"
                "如果候选病名或亚型暗示某个特定基因突变，请判断当前患者本人的分子证据是否支持该主张。"
                "没有患者本人的变异证据时，即使临床综合征合理，基因特异性结论也必须标为未确认。\n\n"
                f"候选病级证据与外部证据：\n{candidate_evidence_block or '- 无'}\n\n"
                f"启发式信号：表型重叠={overlap}，支持性公共相似病例={support_case_count}"
            )
            raw = self._reasoner.complete(
                "你正在审核某个候选疾病是否被当前证据充分支持。请区分临床支持、分子确认、反对证据和缺失信息。",
                prompt,
            )
            parsed = _safe_json_loads(raw or "")
            if isinstance(parsed, dict):
                evidence_ids = parsed.get("evidence_ids", [])
                if not isinstance(evidence_ids, list):
                    evidence_ids = []
                supporting_evidence = parsed.get("supporting_evidence", [])
                if not isinstance(supporting_evidence, list):
                    supporting_evidence = []
                contradicting_evidence = parsed.get("contradicting_evidence", [])
                if not isinstance(contradicting_evidence, list):
                    contradicting_evidence = []
                missing_evidence = parsed.get("missing_evidence", [])
                if not isinstance(missing_evidence, list):
                    missing_evidence = []
                reviews.append(
                    CandidateReview(
                        candidate_name=candidate.normalized_name,
                        is_supported=bool(parsed.get("is_supported", heuristic_supported)),
                        confidence=_safe_float(parsed.get("confidence"), heuristic_confidence),
                        reasoning=_truncate(str(parsed.get("reasoning", "")), 1200)
                        or (
                            f"LLM 复核兜底：疾病知识中的表型重叠数={overlap}，"
                            f"支持性的公共相似病例数={support_case_count}。"
                        ),
                        evidence_ids=[str(item) for item in evidence_ids[:8]]
                        or [item.source_id for item in (candidate_evidence + knowledge_evidence)[:6]],
                        supporting_evidence=[
                            _normalize_text(str(item))
                            for item in supporting_evidence[:8]
                            if _normalize_text(str(item))
                        ],
                        contradicting_evidence=[
                            _normalize_text(str(item))
                            for item in contradicting_evidence[:8]
                            if _normalize_text(str(item))
                        ],
                        missing_evidence=[
                            _normalize_text(str(item))
                            for item in missing_evidence[:8]
                            if _normalize_text(str(item))
                        ],
                    )
                )
            else:
                reviews.append(
                    CandidateReview(
                        candidate_name=candidate.normalized_name,
                        is_supported=heuristic_supported,
                        confidence=heuristic_confidence,
                        reasoning=(
                            f"启发式复核：疾病知识中的表型重叠数={overlap}，"
                            f"支持性的公共相似病例数={support_case_count}。"
                        ),
                        evidence_ids=[item.source_id for item in (candidate_evidence + knowledge_evidence)[:6]],
                        supporting_evidence=[
                            item.source_id for item in candidate_evidence[:4]
                        ],
                        missing_evidence=[
                            _UNCONFIRMED_MOLECULAR_CLAIM_NOTE
                        ]
                        if molecular_assertion_without_evidence
                        else [],
                    )
                )
        return self._localize_reviews_for_chinese_display(reviews)


@dataclass(slots=True)
class LlmFinalDiagnosisSynthesizer:
    """Final physician-facing synthesis over first-pass candidates and reviews."""

    api_key: str
    model_name: str
    base_url: str = ""
    request_timeout_seconds: int = 300
    _reasoner: _OpenAIReasoner | None = None

    def _ensure_reasoner(self) -> None:
        if self._reasoner is None:
            self._reasoner = _OpenAIReasoner(
                self.api_key,
                self.model_name,
                self.base_url,
                timeout=self.request_timeout_seconds,
            )

    @staticmethod
    def _clamp_score(value: float | None, *, default: float = 0.0) -> float:
        score = _safe_float(value, default)
        return max(0.0, min(1.0, score))

    @classmethod
    def _support_level(cls, diagnosis_match_score: float | None) -> str:
        value = cls._clamp_score(diagnosis_match_score)
        if value >= 0.9:
            return "高"
        if value >= 0.7:
            return "中"
        return "低"

    @classmethod
    def _diagnosis_match_percent(cls, diagnosis_match_score: float | None) -> int:
        return int(round(cls._clamp_score(diagnosis_match_score) * 100))

    @staticmethod
    def _as_clean_list(value: object, limit: int = 8) -> list[str]:
        if isinstance(value, list):
            items = value
        elif value:
            items = [value]
        else:
            items = []
        return [
            _normalize_text(str(item))
            for item in items[:limit]
            if _normalize_text(str(item))
        ]

    @classmethod
    def _evidence_strength_score(cls, review: CandidateReview | None) -> float:
        if review is None:
            return 0.35
        support_count = len(review.supporting_evidence or [])
        contradict_count = len(review.contradicting_evidence or [])
        missing_count = len(review.missing_evidence or [])
        evidence_id_count = len(review.evidence_ids or [])
        score = (
            0.35
            + 0.10 * min(support_count, 4)
            + 0.04 * min(evidence_id_count, 4)
            - 0.10 * min(contradict_count, 3)
            - 0.04 * min(missing_count, 4)
        )
        if not review.is_supported:
            score = min(score, 0.45)
        return cls._clamp_score(score, default=0.35)

    @classmethod
    def _compute_diagnosis_match_score(
        cls,
        candidate: CandidateCondition,
        review: CandidateReview | None,
        normalized: NormalizedDisease | None,
    ) -> tuple[float, str]:
        review_confidence = cls._clamp_score(
            review.confidence if review and review.confidence is not None else (
                0.6 if review and review.is_supported else candidate.score
            ),
            default=0.0,
        )
        candidate_score = cls._clamp_score(candidate.score, default=0.0)
        normalization_confidence = cls._clamp_score(
            normalized.normalization_decision_confidence
            if normalized and normalized.normalization_decision_confidence is not None
            else (normalized.mapping_score if normalized and normalized.mapping_score is not None else 0.45),
            default=0.45,
        )
        evidence_strength = cls._evidence_strength_score(review)
        match_score = (
            0.55 * review_confidence
            + 0.20 * candidate_score
            + 0.15 * normalization_confidence
            + 0.10 * evidence_strength
        )
        if review and not review.is_supported:
            match_score = min(match_score, 0.49)
        match_score = cls._clamp_score(match_score)
        reason = (
            f"复核置信度={review_confidence:.2f}，初轮候选分={candidate_score:.2f}，"
            f"标准化置信度={normalization_confidence:.2f}，证据强度={evidence_strength:.2f}。"
        )
        return match_score, reason

    @classmethod
    def _top_final_diagnosis_confidence(cls, diagnosis_cards: list[dict[str, object]]) -> tuple[float, int]:
        if not diagnosis_cards:
            return 0.0, 0
        top = diagnosis_cards[0]
        confidence = cls._clamp_score(top.get("confidence"), default=0.0)
        return confidence, int(round(confidence * 100))

    @staticmethod
    def _needs_chinese_localization(card: dict[str, object]) -> bool:
        zh_name = _normalize_text(str(card.get("disease_name_zh", "")))
        return bool(zh_name) and not _contains_cjk(zh_name)

    @staticmethod
    def _card_content_needs_chinese_localization(card: dict[str, object]) -> bool:
        values: list[str] = [
            _normalize_text(str(card.get("clinical_diagnosis", ""))),
            _normalize_text(str(card.get("inheritance", ""))),
            _normalize_text(str(card.get("molecular_mechanism", ""))),
            _normalize_text(str(card.get("pathogenesis", ""))),
        ]
        for key in [
            "specialties",
            "supporting_evidence",
            "contradicting_evidence",
            "missing_evidence",
            "recommended_tests",
        ]:
            values.extend(_normalize_text(str(item)) for item in card.get(key, []) if item)
        return any(_contains_latin_text(value) for value in values if value)

    def _localize_diagnosis_cards(
        self,
        diagnosis_cards: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        pending = [
            (index, card)
            for index, card in enumerate(diagnosis_cards)
            if self._needs_chinese_localization(card)
        ]
        if not pending or not self.api_key:
            return diagnosis_cards

        self._ensure_reasoner()
        if self._reasoner is None:
            return diagnosis_cards

        candidate_lines = []
        for index, card in pending:
            candidate_lines.append(
                {
                    "index": index,
                    "clinical_diagnosis": _normalize_text(str(card.get("clinical_diagnosis", ""))),
                    "disease_name_zh": _normalize_text(str(card.get("disease_name_zh", ""))),
                    "disease_name_en": _normalize_text(str(card.get("disease_name_en", ""))),
                    "omim_id": _normalize_text(str(card.get("omim_id", ""))),
                    "orphanet_id": _normalize_text(str(card.get("orphanet_id", ""))),
                }
            )

        prompt = (
            "只返回 JSON，不要输出 Markdown 或额外解释。JSON 结构为 "
            "{\"cards\":[{\"index\":0,\"disease_name_zh\":\"中文病名\",\"disease_name_en\":\"English name\"}]}。\n\n"
            "任务：把疾病名整理成适合中文医生阅读的展示形式。\n"
            "规则：\n"
            "1. disease_name_zh 必须尽量输出自然、简洁、专业的中文病名。\n"
            "2. 如果没有统一标准译名，请给出医生能理解的中文译名，不要保留纯英文。\n"
            "3. disease_name_en 保留英文标准名；如果输入里已有合适英文名，尽量保留。\n"
            "4. 不要扩写病情，不要加入解释性句子，只输出疾病名。\n"
            "5. 同一疾病的中文表达前后一致。\n\n"
            f"待处理疾病列表：\n{json.dumps(candidate_lines, ensure_ascii=False)}"
        )
        raw = self._reasoner.complete(
            "你正在为中文临床产品整理疾病展示名。目标是为每个候选疾病生成稳定、自然、专业的中文病名，并保留英文名。",
            prompt,
            temperature=0.0,
            seed=42,
        )
        parsed = _safe_json_loads(raw or "")
        if not isinstance(parsed, dict):
            return diagnosis_cards
        localized_items = parsed.get("cards", [])
        if not isinstance(localized_items, list):
            return diagnosis_cards

        localized_by_index: dict[int, dict[str, str]] = {}
        for item in localized_items:
            if not isinstance(item, dict):
                continue
            try:
                index = int(item.get("index"))
            except Exception:
                continue
            localized_by_index[index] = {
                "disease_name_zh": _normalize_text(str(item.get("disease_name_zh", ""))),
                "disease_name_en": _normalize_text(str(item.get("disease_name_en", ""))),
            }

        merged_cards: list[dict[str, object]] = []
        for index, card in enumerate(diagnosis_cards):
            localized = localized_by_index.get(index)
            if not localized:
                merged_cards.append(card)
                continue
            updated = dict(card)
            localized_zh = localized.get("disease_name_zh", "")
            localized_en = localized.get("disease_name_en", "")
            if localized_zh and _contains_cjk(localized_zh):
                updated["disease_name_zh"] = localized_zh
            if localized_en:
                updated["disease_name_en"] = localized_en
            elif _normalize_text(str(updated.get("disease_name_en", ""))) == "" and not _contains_cjk(
                _normalize_text(str(updated.get("clinical_diagnosis", "")))
            ):
                updated["disease_name_en"] = _normalize_text(str(updated.get("clinical_diagnosis", "")))
            merged_cards.append(updated)
        return merged_cards

    def _localize_diagnosis_card_content(
        self,
        diagnosis_cards: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        pending = [
            (index, card)
            for index, card in enumerate(diagnosis_cards)
            if self._card_content_needs_chinese_localization(card)
        ]
        if not pending or not self.api_key:
            return diagnosis_cards

        self._ensure_reasoner()
        if self._reasoner is None:
            return diagnosis_cards

        payload = []
        for index, card in pending:
            payload.append(
                {
                    "index": index,
                    "disease_name_zh": _normalize_text(str(card.get("disease_name_zh", ""))),
                    "disease_name_en": _normalize_text(str(card.get("disease_name_en", ""))),
                    "clinical_diagnosis": _normalize_text(str(card.get("clinical_diagnosis", ""))),
                    "inheritance": _normalize_text(str(card.get("inheritance", ""))),
                    "molecular_mechanism": _normalize_text(str(card.get("molecular_mechanism", ""))),
                    "pathogenesis": _normalize_text(str(card.get("pathogenesis", ""))),
                    "specialties": list(card.get("specialties", [])),
                    "supporting_evidence": list(card.get("supporting_evidence", [])),
                    "contradicting_evidence": list(card.get("contradicting_evidence", [])),
                    "missing_evidence": list(card.get("missing_evidence", [])),
                    "recommended_tests": list(card.get("recommended_tests", [])),
                }
            )

        prompt = (
            "只返回 JSON，不要输出 Markdown 或额外解释。JSON 结构为 "
            "{\"cards\":[{\"index\":0,\"clinical_diagnosis\":\"中文临床诊断\","
            "\"inheritance\":\"...\",\"molecular_mechanism\":\"...\",\"pathogenesis\":\"...\","
            "\"specialties\":[\"...\"],\"supporting_evidence\":[\"...\"],"
            "\"contradicting_evidence\":[\"...\"],\"missing_evidence\":[\"...\"],"
            "\"recommended_tests\":[\"...\"]}]}。\n\n"
            "任务：把最终诊断卡中的医生展示内容统一整理成中文。\n"
            "规则：\n"
            "1. clinical_diagnosis、inheritance、molecular_mechanism、pathogenesis、specialties、"
            "supporting_evidence、contradicting_evidence、missing_evidence、recommended_tests 必须以中文为主。\n"
            "2. HPO、OMIM、Orphanet、基因名、文献标题、疾病英文缩写可保留英文，但不要整句使用英文。\n"
            "3. 只做展示语言整理，不新增事实、不改变医学结论和证据方向。\n"
            "4. 列表条目尽量简洁，适合中文医生直接阅读。\n\n"
            f"待处理诊断卡：\n{json.dumps(payload, ensure_ascii=False)}"
        )
        raw = self._reasoner.complete(
            "你正在为中文临床产品整理诊断卡展示内容。目标是保留原始医学含义，同时把展示字段稳定转换为中文。",
            prompt,
            temperature=0.0,
            seed=42,
        )
        parsed = _safe_json_loads(raw or "")
        if not isinstance(parsed, dict):
            return diagnosis_cards
        localized_items = parsed.get("cards", [])
        if not isinstance(localized_items, list):
            return diagnosis_cards

        localized_by_index: dict[int, dict[str, object]] = {}
        for item in localized_items:
            if not isinstance(item, dict):
                continue
            try:
                index = int(item.get("index"))
            except Exception:
                continue
            localized_by_index[index] = item

        updated_cards: list[dict[str, object]] = []
        for index, card in enumerate(diagnosis_cards):
            localized = localized_by_index.get(index)
            if not localized:
                updated_cards.append(card)
                continue
            updated = dict(card)
            for field_name in ["clinical_diagnosis", "inheritance", "molecular_mechanism", "pathogenesis"]:
                value = _normalize_text(str(localized.get(field_name, "")))
                if value:
                    updated[field_name] = value
            for field_name in [
                "specialties",
                "supporting_evidence",
                "contradicting_evidence",
                "missing_evidence",
                "recommended_tests",
            ]:
                values = [
                    _normalize_text(str(v))
                    for v in localized.get(field_name, [])[:8]
                    if _normalize_text(str(v))
                ]
                if values:
                    updated[field_name] = values
            updated_cards.append(updated)
        return updated_cards

    def _fallback_diagnosis_cards(
        self,
        sorted_candidates: list[CandidateCondition],
        normalized_candidates: list[NormalizedDisease],
        reviews: list[CandidateReview],
        knowledge_evidence: list[EvidenceItem],
        has_patient_molecular_evidence: bool,
    ) -> list[dict[str, object]]:
        review_by_name = {_normalize_key(item.candidate_name): item for item in reviews}
        normalized_by_name: dict[str, NormalizedDisease] = {}
        for item in normalized_candidates:
            normalized_by_name[_normalize_key(item.original_name)] = item
            normalized_by_name[_normalize_key(item.normalized_name)] = item
        evidence_by_candidate: dict[str, list[EvidenceItem]] = {}
        for item in knowledge_evidence:
            candidate_name = item.metadata.get("candidate", "")
            if candidate_name:
                evidence_by_candidate.setdefault(_normalize_key(candidate_name), []).append(item)
        cards: list[dict[str, object]] = []
        for candidate in sorted_candidates[:5]:
            normalized = normalized_by_name.get(_normalize_key(candidate.name))
            review = review_by_name.get(_normalize_key(candidate.name))
            if normalized and not review:
                review = review_by_name.get(_normalize_key(normalized.normalized_name))
            if normalized and not review:
                review = review_by_name.get(_normalize_key(normalized.original_name))
            if review and not normalized:
                normalized = normalized_by_name.get(_normalize_key(review.candidate_name))
            candidate_evidence = evidence_by_candidate.get(_normalize_key(candidate.name), [])
            if normalized:
                candidate_evidence.extend(evidence_by_candidate.get(_normalize_key(normalized.normalized_name), []))
            confidence = review.confidence if review else candidate.score
            diagnosis_match_score, ranking_reason = self._compute_diagnosis_match_score(
                candidate,
                review,
                normalized,
            )
            missing_evidence = list(review.missing_evidence if review else [])
            if not has_patient_molecular_evidence and _contains_molecular_assertion(candidate.name):
                missing_evidence.append(_UNCONFIRMED_MOLECULAR_CLAIM_NOTE)
            omim_ids = self._card_omim_ids(candidate, normalized, candidate_evidence)
            orphanet_id = normalized.disease_id if normalized and normalized.disease_id else ""
            disease_name_zh = candidate.name if _contains_cjk(candidate.name) else candidate.name
            disease_name_en = ""
            if normalized and not _contains_cjk(normalized.normalized_name):
                disease_name_en = normalized.normalized_name
            elif not _contains_cjk(candidate.name):
                disease_name_en = candidate.name
            cards.append(
                {
                    "rank": 0,
                    "diagnosis_match_score": diagnosis_match_score,
                    "diagnosis_match_percent": self._diagnosis_match_percent(diagnosis_match_score),
                    "disease_name_zh": disease_name_zh,
                    "disease_name_en": disease_name_en,
                    "clinical_diagnosis": candidate.name,
                    "support_level": self._support_level(diagnosis_match_score),
                    "confidence": self._clamp_score(confidence, default=0.0),
                    "ranking_reason": ranking_reason,
                    "omim_id": omim_ids[0] if omim_ids else "NA",
                    "omim_url": f"https://www.omim.org/entry/{omim_ids[0]}" if omim_ids else "",
                    "orphanet_id": orphanet_id or "NA",
                    "orphanet_url": self._first_evidence_url(candidate_evidence, "orphanet"),
                    "inheritance": "待从 OMIM/Orphanet/文献证据中确认",
                    "disease_genes": self._extract_gene_like_terms(candidate.name + " " + candidate.rationale),
                    "molecular_mechanism": "待从 OMIM/Orphanet/文献证据中确认。",
                    "pathogenesis": "待从候选病级证据中归纳。",
                    "specialties": ["生殖遗传", "遗传科"],
                    "supporting_evidence": (
                        list(review.supporting_evidence if review else [])
                        or list(review.evidence_ids[:4] if review else [])
                        or candidate.supporting_phenotypes[:4]
                    ),
                    "contradicting_evidence": list(review.contradicting_evidence if review else []),
                    "missing_evidence": list(dict.fromkeys(missing_evidence)),
                    "recommended_tests": [
                        "补充或确认关键 HPO/临床表型",
                        "结合候选病特点选择基因检测、内分泌、生化、影像或生殖史复核",
                    ],
                    "references": self._card_references(candidate_evidence),
                    "cautions": [
                        "该卡片为诊断辅助结论，不能替代医生最终诊断。",
                        "没有当前患者分子结果时，不应把候选病写成已确认基因病因。",
                    ],
                }
            )
        cards.sort(
            key=lambda item: (
                self._clamp_score(item.get("diagnosis_match_score"), default=0.0),
                self._clamp_score(item.get("confidence"), default=0.0),
            ),
            reverse=True,
        )
        for index, card in enumerate(cards, start=1):
            card["rank"] = index
        return cards

    @staticmethod
    def _normalize_reference_source_type(source_type: str, url: str = "", title: str = "") -> str:
        normalized = _normalize_text(source_type).lower()
        if normalized in {"pubmed", "omim", "orphanet", "web_search"}:
            return normalized
        url_lower = (url or "").lower()
        title_lower = (title or "").lower()
        if "pubmed.ncbi.nlm.nih.gov" in url_lower or "pmid" in title_lower:
            return "pubmed"
        if "omim.org" in url_lower:
            return "omim"
        if "orpha.net" in url_lower or "orphanet" in title_lower:
            return "orphanet"
        return "web_search"

    @staticmethod
    def _extract_gene_like_terms(text: str) -> list[str]:
        genes = re.findall(r"\b[A-Z0-9]{2,12}\b", text or "")
        ignored = {
            "OMIM",
            "ORPHA",
            "HPO",
            "HP",
            "DNA",
            "RNA",
            "WES",
            "WGS",
            "IVF",
            "ICSI",
            "RHM",
            "NA",
        }
        return [
            gene
            for gene in dict.fromkeys(genes)
            if gene not in ignored and not gene.isdigit()
        ][:8]

    @staticmethod
    def _card_omim_ids(
        candidate: CandidateCondition,
        normalized: NormalizedDisease | None,
        evidence: list[EvidenceItem],
    ) -> list[str]:
        chunks = [candidate.name, candidate.rationale]
        if normalized:
            chunks.extend([normalized.disease_id or "", normalized.ontology, normalized.normalized_name])
        chunks.extend([item.source_id + " " + item.title + " " + item.url for item in evidence])
        return list(dict.fromkeys(re.findall(r"(?:OMIM:?|omim-|entry/)(\d{3,})", " ".join(chunks), re.IGNORECASE)))

    @staticmethod
    def _first_evidence_url(evidence: list[EvidenceItem], source_type: str) -> str:
        for item in evidence:
            if item.source_type == source_type and item.url:
                return item.url
        return ""

    @staticmethod
    def _card_references(evidence: list[EvidenceItem]) -> list[dict[str, str]]:
        references: list[dict[str, str]] = []
        for item in evidence[:8]:
            references.append(
                {
                    "title": item.title,
                    "source_type": LlmFinalDiagnosisSynthesizer._normalize_reference_source_type(
                        item.source_type,
                        item.url,
                        item.title,
                    ),
                    "url": item.url,
                    "citation": item.citation,
                }
            )
        return references

    @staticmethod
    def _find_fallback_card(
        item: dict[str, object],
        fallback_cards: list[dict[str, object]],
    ) -> dict[str, object] | None:
        keys = [
            _normalize_key(_normalize_text(str(item.get("clinical_diagnosis", "")))),
            _normalize_key(_normalize_text(str(item.get("disease_name_zh", "")))),
            _normalize_key(_normalize_text(str(item.get("disease_name_en", "")))),
        ]
        keys = [key for key in keys if key]
        if not keys:
            return None
        for card in fallback_cards:
            card_keys = [
                _normalize_key(_normalize_text(str(card.get("clinical_diagnosis", "")))),
                _normalize_key(_normalize_text(str(card.get("disease_name_zh", "")))),
                _normalize_key(_normalize_text(str(card.get("disease_name_en", "")))),
            ]
            if any(key and key in card_keys for key in keys):
                return card
        return None

    def _merge_llm_cards_with_canonical_ranking(
        self,
        raw_cards: list[dict[str, object]],
        fallback_cards: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        if not raw_cards:
            return fallback_cards
        merged_cards: list[dict[str, object]] = []
        consumed_ids: set[int] = set()
        for fallback_card in fallback_cards:
            matched_card: dict[str, object] | None = None
            matched_index: int | None = None
            fallback_probe = {
                "clinical_diagnosis": fallback_card.get("clinical_diagnosis", ""),
                "disease_name_zh": fallback_card.get("disease_name_zh", ""),
                "disease_name_en": fallback_card.get("disease_name_en", ""),
            }
            for index, raw_card in enumerate(raw_cards):
                if index in consumed_ids:
                    continue
                if self._find_fallback_card(raw_card, [fallback_card]) is not None:
                    matched_card = raw_card
                    matched_index = index
                    break
                if self._find_fallback_card(fallback_probe, [raw_card]) is not None:
                    matched_card = raw_card
                    matched_index = index
                    break
            if matched_card is None:
                merged_cards.append(dict(fallback_card))
                continue
            consumed_ids.add(matched_index if matched_index is not None else -1)
            merged = dict(fallback_card)
            merged.update(
                {
                    "disease_name_zh": _normalize_text(str(matched_card.get("disease_name_zh", "")))
                    or str(fallback_card.get("disease_name_zh", "")),
                    "disease_name_en": _normalize_text(str(matched_card.get("disease_name_en", "")))
                    or str(fallback_card.get("disease_name_en", "")),
                    "clinical_diagnosis": _normalize_text(str(matched_card.get("clinical_diagnosis", "")))
                    or str(fallback_card.get("clinical_diagnosis", "")),
                    "inheritance": _normalize_text(str(matched_card.get("inheritance", "")))
                    or str(fallback_card.get("inheritance", "NA")),
                    "disease_genes": self._as_clean_list(matched_card.get("disease_genes", []))
                    or list(fallback_card.get("disease_genes", [])),
                    "molecular_mechanism": _normalize_text(str(matched_card.get("molecular_mechanism", "")))
                    or str(fallback_card.get("molecular_mechanism", "NA")),
                    "pathogenesis": _normalize_text(str(matched_card.get("pathogenesis", "")))
                    or str(fallback_card.get("pathogenesis", "")),
                    "specialties": self._as_clean_list(matched_card.get("specialties", []))
                    or list(fallback_card.get("specialties", [])),
                    "supporting_evidence": self._as_clean_list(matched_card.get("supporting_evidence", []))
                    or list(fallback_card.get("supporting_evidence", [])),
                    "contradicting_evidence": self._as_clean_list(matched_card.get("contradicting_evidence", []))
                    or list(fallback_card.get("contradicting_evidence", [])),
                    "missing_evidence": self._as_clean_list(matched_card.get("missing_evidence", []))
                    or list(fallback_card.get("missing_evidence", [])),
                    "recommended_tests": self._as_clean_list(matched_card.get("recommended_tests", []))
                    or list(fallback_card.get("recommended_tests", [])),
                    "references": [
                        {
                            "title": _normalize_text(str(ref.get("title", ""))),
                            "source_type": self._normalize_reference_source_type(
                                _normalize_text(str(ref.get("source_type", ""))),
                                _normalize_text(str(ref.get("url", ""))),
                                _normalize_text(str(ref.get("title", ""))),
                            ),
                            "url": _normalize_text(str(ref.get("url", ""))),
                            "citation": _normalize_text(str(ref.get("citation", ""))),
                        }
                        for ref in matched_card.get("references", [])[:8]
                        if isinstance(ref, dict)
                    ]
                    if isinstance(matched_card.get("references", []), list)
                    else list(fallback_card.get("references", [])),
                    "cautions": self._as_clean_list(matched_card.get("cautions", []))
                    or list(fallback_card.get("cautions", [])),
                }
            )
            merged_cards.append(merged)
        return merged_cards

    def synthesize(
        self,
        patient: PatientProfile,
        phenotypes: list[PhenotypeItem],
        phenotype_hints: list[PhenotypeToolHit],
        knowledge_evidence: list[EvidenceItem],
        similar_cases: list[SimilarCase],
        initial_candidates: list[CandidateCondition],
        normalized_candidates: list[NormalizedDisease],
        reviews: list[CandidateReview],
    ) -> TraceableRecommendation:
        has_patient_molecular_evidence = _patient_has_molecular_evidence(patient)
        molecular_policy = _molecular_evidence_policy(has_patient_molecular_evidence)
        review_by_name = { _normalize_key(item.candidate_name): item for item in reviews }
        for normalized in normalized_candidates:
            normalized_key = _normalize_key(normalized.normalized_name)
            original_key = _normalize_key(normalized.original_name)
            review = review_by_name.get(normalized_key)
            if review and original_key:
                review_by_name[original_key] = review
        sorted_candidates = sorted(
            initial_candidates,
            key=lambda item: (
                review_by_name.get(_normalize_key(item.name), CandidateReview(item.name, False)).is_supported,
                review_by_name.get(_normalize_key(item.name), CandidateReview(item.name, False)).confidence or 0.0,
                item.score or 0.0,
            ),
            reverse=True,
        )

        fallback_summary = (
            "Clinical MVP 已按 DeepRare 风格完成一轮可追溯鉴别诊断。"
            "该结果用于开发验证，仍需不孕不育/生殖遗传领域专家复核。"
        )
        fallback_next_steps = [
            "结合生育史、内分泌检查、影像学结果和既往妊娠结局复核优先候选。",
            "确认当前 HPO 表型是否完整、准确，必要时补充关键阴性和阳性表型。",
            "核对候选疾病是否与在线证据、公共确诊病例和本地检测案例一致。",
        ]
        fallback_cautions = [
            "当前结果依赖在线服务和病例库检索，检索失败或证据不足会影响排序。",
            "疾病复核为启发式/LLM 辅助判断，不能替代医生的最终临床诊断。",
        ]
        fallback_cards = self._fallback_diagnosis_cards(
            sorted_candidates,
            normalized_candidates,
            reviews,
            knowledge_evidence,
            has_patient_molecular_evidence,
        )
        localized_fallback_cards = self._localize_diagnosis_cards(fallback_cards) if self.api_key else fallback_cards
        localized_fallback_cards = self._localize_diagnosis_card_content(localized_fallback_cards)
        fallback_final_confidence, fallback_final_confidence_percent = self._top_final_diagnosis_confidence(
            localized_fallback_cards
        )

        if not self.api_key:
            return TraceableRecommendation(
                summary=fallback_summary,
                candidates=sorted_candidates,
                evidence=knowledge_evidence,
                reviews=reviews,
                next_steps=fallback_next_steps,
                cautions=fallback_cautions,
                final_diagnosis_confidence=fallback_final_confidence,
                final_diagnosis_confidence_percent=fallback_final_confidence_percent,
                diagnosis_cards=localized_fallback_cards,
            )

        self._ensure_reasoner()
        candidate_block = "\n".join(
            f"- {cand.name}: 分数={cand.score}; 理由={_truncate(cand.rationale, 200)}"
            for cand in sorted_candidates[:5]
        )
        review_block = "\n".join(
            f"- {review.candidate_name}: 是否支持={review.is_supported}; 置信度={review.confidence}; { _truncate(review.reasoning, 220) }"
            for review in reviews[:5]
        )
        evidence_block = "\n".join(
            f"- {item.source_id}; type={item.source_type}; url={item.url or '-'}; title={item.title}: {_truncate(item.summary, 220)}"
            for item in knowledge_evidence[:10]
        )
        normalized_block = "\n".join(
            f"- original={item.original_name}; normalized={item.normalized_name}; id={item.disease_id or 'NA'}; ontology={item.ontology or 'NA'}"
            for item in normalized_candidates[:5]
        )
        prompt = (
            "只返回 JSON，不要输出 Markdown 或额外解释。JSON 结构为 "
            "{\"summary\":\"...\",\"diagnosis_cards\":[{\"disease_name_zh\":\"中文疾病名\","
            "\"disease_name_en\":\"English disease name or empty string\",\"clinical_diagnosis\":\"...\","
            "\"support_level\":\"高|中|低\",\"confidence\":0.0,"
            "\"omim_id\":\"真实 OMIM 号或 NA\",\"omim_url\":\"真实 OMIM 链接或空字符串\","
            "\"orphanet_id\":\"ORPHA:xxx 或 NA\",\"orphanet_url\":\"...\","
            "\"inheritance\":\"...\",\"disease_genes\":[\"...\"],\"molecular_mechanism\":\"...\","
            "\"pathogenesis\":\"...\",\"specialties\":[\"...\"],\"supporting_evidence\":[\"...\"],"
            "\"contradicting_evidence\":[\"...\"],\"missing_evidence\":[\"...\"],"
            "\"recommended_tests\":[\"...\"],\"references\":[{\"title\":\"...\",\"source_type\":\"omim|pubmed|orphanet\",\"url\":\"...\",\"citation\":\"...\"}],"
            "\"cautions\":[\"...\"]}],"
            "\"next_steps\":[\"...\"],\"cautions\":[\"...\"]}.\n\n"
            "输出语言要求：summary、diagnosis_cards、next_steps、cautions 必须以中文为主；疾病英文名、HPO、OMIM、"
            "PubMed 标题、基因名等必要英文术语可保留英文，但不要直接输出整句英文。\n\n"
            "疾病名展示要求：disease_name_zh 必须尽量给出中文疾病名。若没有标准中文译名，"
            "请给出医生能理解的中文译名并在 disease_name_en 保留英文原名；"
            "不要只把英文疾病名放在 disease_name_zh 中。\n\n"
            "诊断卡展示要求：clinical_diagnosis、inheritance、molecular_mechanism、pathogenesis、"
            "specialties、supporting_evidence、contradicting_evidence、missing_evidence、recommended_tests "
            "也必须以中文为主；可保留 HPO、OMIM、PubMed 标题和基因名，但不要整句保留英文。\n\n"
            "疾病分子信息要求：disease_genes 和 molecular_mechanism 表示该疾病在知识库中的致病基因、"
            "染色体区域或分子/遗传机制，不代表当前患者已经检出相关异常。"
            "如果证据不足，填写 NA 或“待确认”。不要输出“分子亚型”概念，也不要把另一个候选疾病当作本病的分子机制。\n\n"
            f"病例信息：\n{patient.narrative()}\n\n"
            f"已确认表型：\n" + "\n".join(f"- {item.label}" for item in phenotypes[:10]) + "\n\n"
            f"分子证据规则：\n{molecular_policy}\n\n"
            "最终输出规则：必须区分临床诊断/鉴别诊断、疾病知识中的致病基因/机制、以及患者本人的检测结果。"
            "如果没有患者本人的变异或核型结果，请使用“疑似”“建议检测”等措辞，"
            "不要写成“由 X 基因导致”。\n\n"
            f"第一轮候选：\n{candidate_block}\n\n"
            f"疾病标准化结果：\n{normalized_block or '- 无'}\n\n"
            f"逐病种复核：\n{review_block}\n\n"
            f"证据摘要：\n{evidence_block or '- 无'}"
        )
        raw = self._reasoner.complete(
            "你正在为中文医生用户生成简洁的诊断辅助摘要。请基于 DeepRare 风格证据链输出可追溯结论，并避免把未确认分子假设表述为已确认病因。",
            prompt,
        )
        parsed = _safe_json_loads(raw or "")
        if isinstance(parsed, dict):
            next_steps = parsed.get("next_steps", [])
            if not isinstance(next_steps, list):
                next_steps = []
            cautions = parsed.get("cautions", [])
            if not isinstance(cautions, list):
                cautions = []
            raw_cards = parsed.get("diagnosis_cards", [])
            diagnosis_cards: list[dict[str, object]] = []
            if isinstance(raw_cards, list):
                for item in raw_cards[:5]:
                    if not isinstance(item, dict):
                        continue
                    fallback_card = self._find_fallback_card(item, fallback_cards)
                    trusted_omim_id = "NA"
                    trusted_omim_url = ""
                    trusted_orphanet_id = "NA"
                    trusted_orphanet_url = ""
                    if fallback_card:
                        trusted_omim_id = (
                            _normalize_text(str(fallback_card.get("omim_id", ""))) or "NA"
                        )
                        trusted_omim_url = _normalize_text(
                            str(fallback_card.get("omim_url", ""))
                        )
                        trusted_orphanet_id = (
                            _normalize_text(str(fallback_card.get("orphanet_id", ""))) or "NA"
                        )
                        trusted_orphanet_url = _normalize_text(
                            str(fallback_card.get("orphanet_url", ""))
                        )
                    clinical_diagnosis = _normalize_text(
                        str(item.get("clinical_diagnosis", ""))
                    )
                    disease_name_zh = _normalize_text(str(item.get("disease_name_zh", "")))
                    disease_name_en = _normalize_text(str(item.get("disease_name_en", "")))
                    if not disease_name_zh:
                        disease_name_zh = clinical_diagnosis
                    if not disease_name_en and disease_name_zh and not _contains_cjk(disease_name_zh):
                        disease_name_en = disease_name_zh
                    diagnosis_cards.append(
                        {
                            "disease_name_zh": disease_name_zh,
                            "disease_name_en": disease_name_en,
                            "clinical_diagnosis": clinical_diagnosis,
                            "support_level": str(fallback_card.get("support_level", "中")) if fallback_card else "中",
                            "confidence": _safe_float(
                                fallback_card.get("confidence", 0.0) if fallback_card else item.get("confidence"),
                                0.0,
                            ),
                            "rank": int(fallback_card.get("rank", 0)) if fallback_card else 0,
                            "diagnosis_match_score": _safe_float(
                                fallback_card.get("diagnosis_match_score", 0.0) if fallback_card else 0.0,
                                0.0,
                            ),
                            "diagnosis_match_percent": int(
                                fallback_card.get("diagnosis_match_percent", 0) if fallback_card else 0
                            ),
                            "ranking_reason": str(fallback_card.get("ranking_reason", "")) if fallback_card else "",
                            "omim_id": trusted_omim_id,
                            "omim_url": trusted_omim_url,
                            "orphanet_id": trusted_orphanet_id,
                            "orphanet_url": trusted_orphanet_url,
                            "inheritance": _normalize_text(str(item.get("inheritance", ""))) or "NA",
                            "disease_genes": self._as_clean_list(item.get("disease_genes", [])),
                            "molecular_mechanism": _normalize_text(
                                str(item.get("molecular_mechanism", ""))
                            )
                            or "NA",
                            "pathogenesis": _normalize_text(str(item.get("pathogenesis", ""))),
                            "specialties": self._as_clean_list(item.get("specialties", [])),
                            "supporting_evidence": self._as_clean_list(
                                item.get("supporting_evidence", [])
                            ),
                            "contradicting_evidence": self._as_clean_list(
                                item.get("contradicting_evidence", [])
                            ),
                            "missing_evidence": self._as_clean_list(
                                item.get("missing_evidence", [])
                            ),
                            "recommended_tests": self._as_clean_list(
                                item.get("recommended_tests", [])
                            ),
                            "references": [
                                {
                                    "title": _normalize_text(str(ref.get("title", ""))),
                                    "source_type": self._normalize_reference_source_type(
                                        _normalize_text(str(ref.get("source_type", ""))),
                                        _normalize_text(str(ref.get("url", ""))),
                                        _normalize_text(str(ref.get("title", ""))),
                                    ),
                                    "url": _normalize_text(str(ref.get("url", ""))),
                                    "citation": _normalize_text(str(ref.get("citation", ""))),
                                }
                                for ref in item.get("references", [])[:8]
                                if isinstance(ref, dict)
                            ]
                            if isinstance(item.get("references", []), list)
                            else [],
                            "cautions": self._as_clean_list(item.get("cautions", [])),
                        }
                    )
            diagnosis_cards = self._merge_llm_cards_with_canonical_ranking(diagnosis_cards, fallback_cards)
            diagnosis_cards = self._localize_diagnosis_cards(diagnosis_cards)
            diagnosis_cards = self._localize_diagnosis_card_content(diagnosis_cards)
            final_confidence, final_confidence_percent = self._top_final_diagnosis_confidence(
                diagnosis_cards or fallback_cards
            )
            return TraceableRecommendation(
                summary=_truncate(str(parsed.get("summary", fallback_summary)), 2000),
                candidates=sorted_candidates,
                evidence=knowledge_evidence,
                reviews=reviews,
                next_steps=[_normalize_text(str(item)) for item in next_steps[:6] if _normalize_text(str(item))]
                or fallback_next_steps,
                cautions=[_normalize_text(str(item)) for item in cautions[:6] if _normalize_text(str(item))]
                or fallback_cautions,
                final_diagnosis_confidence=final_confidence,
                final_diagnosis_confidence_percent=final_confidence_percent,
                diagnosis_cards=diagnosis_cards or fallback_cards,
            )

        return TraceableRecommendation(
            summary=fallback_summary,
            candidates=sorted_candidates,
            evidence=knowledge_evidence,
            reviews=reviews,
            next_steps=fallback_next_steps,
            cautions=fallback_cautions,
            final_diagnosis_confidence=fallback_final_confidence,
            final_diagnosis_confidence_percent=fallback_final_confidence_percent,
            diagnosis_cards=localized_fallback_cards,
        )


class StubInitialDiagnosisSynthesizer:
    """Placeholder first-round diagnosis synthesizer."""

    def synthesize(
        self,
        patient: PatientProfile,
        phenotypes: list[PhenotypeItem],
        phenotype_hints: list[PhenotypeToolHit],
        knowledge_evidence: list[EvidenceItem],
        similar_cases: list[SimilarCase],
        top_k: int,
    ) -> list[CandidateCondition]:
        candidates = [
            CandidateCondition(
                name="需要接入领域诊断综合模块",
                rank=1,
                score=0.2,
                rationale=(
                    "第一轮候选占位。请替换为 DeepRare 风格两阶段综合：先仅基于病例做 LLM 初判，"
                    "再结合检索证据和相似病例做增强综合。"
                ),
                supporting_phenotypes=[item.label for item in phenotypes[:5]],
            )
        ]
        return candidates[:top_k]


class StubDiseaseNormalizer:
    """Placeholder disease normalization stage."""

    def normalize(
        self,
        candidates: list[CandidateCondition],
    ) -> list[NormalizedDisease]:
        return [
            NormalizedDisease(
                original_name=candidate.name,
                normalized_name=candidate.name,
                ontology="placeholder",
                mapping_score=0.1,
            )
            for candidate in candidates
        ]


class StubPerDiseaseVerifier:
    """Placeholder per-candidate verification stage."""

    def verify(
        self,
        patient: PatientProfile,
        phenotypes: list[PhenotypeItem],
        similar_cases: list[SimilarCase],
        knowledge_evidence: list[EvidenceItem],
        normalized_candidates: list[NormalizedDisease],
    ) -> list[CandidateReview]:
        evidence_ids = [item.source_id for item in knowledge_evidence]
        return [
            CandidateReview(
                candidate_name=candidate.normalized_name,
                is_supported=True,
                confidence=0.2,
                reasoning=(
                    "复核占位，模拟 DeepRare 的逐病种校验阶段。请替换为病种证据收集、"
                    "支持证据和反对证据检查。"
                ),
                evidence_ids=evidence_ids,
            )
            for candidate in normalized_candidates
        ]


class StubFinalDiagnosisSynthesizer:
    """Placeholder final synthesis stage."""

    def synthesize(
        self,
        patient: PatientProfile,
        phenotypes: list[PhenotypeItem],
        phenotype_hints: list[PhenotypeToolHit],
        knowledge_evidence: list[EvidenceItem],
        similar_cases: list[SimilarCase],
        initial_candidates: list[CandidateCondition],
        normalized_candidates: list[NormalizedDisease],
        reviews: list[CandidateReview],
    ) -> TraceableRecommendation:
        top_candidate = initial_candidates[0] if initial_candidates else None
        top_score = _safe_float(top_candidate.score if top_candidate else 0.0, 0.0)
        return TraceableRecommendation(
            summary=(
                "Clinical MVP 已按 DeepRare 风格阶段顺序组装占位输出。"
                "该结果尚不能作为临床可用建议。"
            ),
            candidates=initial_candidates,
            evidence=knowledge_evidence,
            reviews=reviews,
            next_steps=[
                "接入本地表型生成智能体。",
                "用本地知识库和病例库检索替换占位检索。",
                "用受控提示链实现第一轮诊断和最终综合。",
                "基于目标不孕不育疾病体系实现疾病标准化。",
            ],
            cautions=[
                "当前 MVP 服务仍是工程骨架，不适合直接用于临床。",
            ],
            final_diagnosis_confidence=top_score,
            final_diagnosis_confidence_percent=int(round(top_score * 100)),
            diagnosis_cards=[
                {
                    "rank": 1,
                    "diagnosis_match_score": top_score,
                    "diagnosis_match_percent": int(round(top_score * 100)),
                    "disease_name_zh": top_candidate.name if top_candidate else "待生成候选诊断",
                    "disease_name_en": "",
                    "clinical_diagnosis": top_candidate.name if top_candidate else "待生成候选诊断",
                    "support_level": "低",
                    "confidence": top_score,
                    "ranking_reason": "占位排序：仅按首个候选分数展示。",
                    "omim_id": "NA",
                    "omim_url": "",
                    "orphanet_id": "NA",
                    "orphanet_url": "",
                    "inheritance": "NA",
                    "disease_genes": [],
                    "molecular_mechanism": "占位输出，尚未接入疾病分子机制归纳。",
                    "pathogenesis": "占位输出，尚未接入疾病发病机制归纳。",
                    "specialties": [],
                    "supporting_evidence": [item.source_id for item in knowledge_evidence[:3]],
                    "contradicting_evidence": [],
                    "missing_evidence": ["尚未接入真实复核证据链。"],
                    "recommended_tests": ["接入真实诊断综合模块后生成。"],
                    "references": [],
                    "cautions": ["占位诊断卡不能用于临床判断。"],
                }
            ],
        )
