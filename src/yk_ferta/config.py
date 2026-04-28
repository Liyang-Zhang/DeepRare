"""Shared configuration for yk-FERTA."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


APP_NAME = "yk-FERTA"
DEFAULT_CONFIG_PATH = Path("config/clinical_mvp.json")
PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(slots=True)
class OpenAIConfig:
    api_key: str = ""
    base_url: str = ""


@dataclass(slots=True)
class PhenotypeExtractorConfig:
    enabled: bool = True
    provider: str = "deeprare"
    model_name: str = "gpt-4.1"
    biolord_model_path: str = "FremyCompany/BioLORD-2023-C"
    concept2id_path: str = "./database/definition2id.json"
    concept_embeddings_path: str = "./database/embeds_pheno.pt"
    similarity_threshold: float = 0.8
    rag_hpo_base_url: str = "http://127.0.0.1:18080"
    rag_hpo_temperature: float = 0.3
    rag_hpo_enable_infertility_filter: bool = False
    rag_hpo_request_timeout_seconds: int = 30
    rag_hpo_poll_interval_seconds: float = 1.0
    rag_hpo_poll_timeout_seconds: int = 120


@dataclass(slots=True)
class KnowledgeSearcherConfig:
    enabled: bool = True
    search_engine: str = "duckduckgo"
    google_api: str = ""
    search_engine_id: str = ""
    chrome_driver: str = "/usr/local/bin/chromedriver"
    visualize: bool = False
    mini_model_name: str = "gpt-4o-mini"
    web_results: int = 3
    pubmed_results: int = 3
    arxiv_results: int = 0
    wiki_results: int = 0


@dataclass(slots=True)
class PhenotypeAnalyserConfig:
    enabled: bool = True
    enable_pubcasefinder: bool = False
    enable_phenobrain: bool = True
    enable_hpo_association: bool = True
    hpo_association_top_n: int = 5


@dataclass(slots=True)
class CaseSearcherConfig:
    enabled: bool = True
    mode: str = "fertility_dual"
    case_bank_path: str = "./database/RDS_embeddings.csv"
    public_case_bank_path: str = "./database/fertility_public_cases_rds.csv"
    private_testing_case_bank_path: str = "./database/fertility_private_testing_cases_2025.with_hpo.csv"
    vector_index_path: str = "./database/fertility_case_vector_index.npz"
    vector_metadata_path: str = "./database/fertility_case_vector_metadata.csv"
    vectorizer_path: str = "./database/fertility_case_vectorizer.joblib"
    embedding_model: str = "text-embedding-3-small"
    top_n: int = 50
    return_k: int = 3
    public_return_k: int = 3
    private_return_k: int = 3
    vector_top_n: int = 200
    vector_weight: float = 0.45
    min_score: float = 0.01
    llm_filter: bool = False
    filter_model_name: str = "gpt-4.1"


@dataclass(slots=True)
class ReasoningConfig:
    model_name: str = "gpt-4.1"
    orphanet_path: str = "./database/orpha_disorders_HP_map.json"
    orpha_concept2id_path: str = "./database/orpha_concept2id.json"
    orpha2name_path: str = "./database/orpha2name.json"
    orpha2omim_path: str = "./database/orpha2omim.json"
    disease_normalization_top_n: int = 5
    disease_normalization_llm_temperature: float = 0.0


@dataclass(slots=True)
class ClinicalMvpConfig:
    openai: OpenAIConfig = field(default_factory=OpenAIConfig)
    phenotype_extractor: PhenotypeExtractorConfig = field(
        default_factory=PhenotypeExtractorConfig
    )
    knowledge_searcher: KnowledgeSearcherConfig = field(
        default_factory=KnowledgeSearcherConfig
    )
    phenotype_analyser: PhenotypeAnalyserConfig = field(
        default_factory=PhenotypeAnalyserConfig
    )
    case_searcher: CaseSearcherConfig = field(default_factory=CaseSearcherConfig)
    reasoning: ReasoningConfig = field(default_factory=ReasoningConfig)

    @classmethod
    def load(cls, path: str | Path = DEFAULT_CONFIG_PATH) -> "ClinicalMvpConfig":
        """Load config from a JSON file, or return defaults if it does not exist."""
        config_path = Path(path)
        if not config_path.is_absolute() and not config_path.exists():
            project_config_path = PROJECT_ROOT / config_path
            if project_config_path.exists():
                config_path = project_config_path
        if not config_path.exists():
            return cls()

        with config_path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)

        return cls(
            openai=OpenAIConfig(**raw.get("openai", {})),
            phenotype_extractor=PhenotypeExtractorConfig(
                **raw.get("phenotype_extractor", {})
            ),
            knowledge_searcher=KnowledgeSearcherConfig(
                **raw.get("knowledge_searcher", {})
            ),
            phenotype_analyser=PhenotypeAnalyserConfig(
                **raw.get("phenotype_analyser", {})
            ),
            case_searcher=CaseSearcherConfig(**raw.get("case_searcher", {})),
            reasoning=ReasoningConfig(**raw.get("reasoning", {})),
        )
