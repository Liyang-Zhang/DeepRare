"""Factory helpers for assembling yk-FERTA pipelines."""

from __future__ import annotations

from yk_ferta.config import ClinicalMvpConfig
from yk_ferta.pipelines.clinical_mvp import ClinicalMvpPipeline
from yk_ferta.pipelines.traceable_diagnosis import TraceableDiagnosisPipeline
from yk_ferta.services.clinical_mvp import (
    DeepRareCaseSearcher,
    DeepRareKnowledgeSearcher,
    DeepRarePhenotypeAnalyser,
    DeepRarePhenotypeExtractor,
    FertilityDualCaseSearcher,
    LlmFinalDiagnosisSynthesizer,
    LlmInitialDiagnosisSynthesizer,
    LlmPerDiseaseVerifier,
    LocalDiseaseNormalizer,
    NarrativePhenotypeExtractor,
    RagHpoPhenotypeExtractor,
    StubCaseSearcher,
    StubDiseaseNormalizer,
    StubFinalDiagnosisSynthesizer,
    StubInitialDiagnosisSynthesizer,
    StubKnowledgeSearcher,
    StubPerDiseaseVerifier,
    StubPhenotypeAnalyser,
)
from yk_ferta.services.defaults import (
    BasicTraceableOutputBuilder,
    HeuristicCandidateGenerator,
    PassthroughPhenotypeStandardizer,
    StubEvidenceRetriever,
    SupportiveEvidenceVerifier,
)


def build_default_pipeline() -> TraceableDiagnosisPipeline:
    """Return the default development pipeline."""
    return TraceableDiagnosisPipeline(
        phenotype_standardizer=PassthroughPhenotypeStandardizer(),
        evidence_retriever=StubEvidenceRetriever(),
        candidate_generator=HeuristicCandidateGenerator(),
        evidence_verifier=SupportiveEvidenceVerifier(),
        output_builder=BasicTraceableOutputBuilder(),
    )


def build_clinical_mvp_pipeline(
    config: ClinicalMvpConfig | None = None,
) -> ClinicalMvpPipeline:
    """Return the DeepRare-style clinical-only MVP pipeline."""
    cfg = config or ClinicalMvpConfig.load()

    narrative_fallback = NarrativePhenotypeExtractor()
    deeprare_fallback = narrative_fallback
    if cfg.phenotype_extractor.enabled and cfg.openai.api_key:
        try:
            deeprare_fallback = DeepRarePhenotypeExtractor(
                api_key=cfg.openai.api_key,
                base_url=cfg.openai.base_url,
                model_name=cfg.phenotype_extractor.model_name,
                biolord_model_path=cfg.phenotype_extractor.biolord_model_path,
                concept2id_path=cfg.phenotype_extractor.concept2id_path,
                concept_embeddings_path=cfg.phenotype_extractor.concept_embeddings_path,
                similarity_threshold=cfg.phenotype_extractor.similarity_threshold,
            )
        except Exception:
            deeprare_fallback = narrative_fallback

    if cfg.phenotype_extractor.enabled and cfg.phenotype_extractor.provider == "rag_hpo":
        phenotype_extractor = RagHpoPhenotypeExtractor(
            base_url=cfg.phenotype_extractor.rag_hpo_base_url,
            temperature=cfg.phenotype_extractor.rag_hpo_temperature,
            enable_infertility_filter=cfg.phenotype_extractor.rag_hpo_enable_infertility_filter,
            request_timeout_seconds=cfg.phenotype_extractor.rag_hpo_request_timeout_seconds,
            poll_interval_seconds=cfg.phenotype_extractor.rag_hpo_poll_interval_seconds,
            poll_timeout_seconds=cfg.phenotype_extractor.rag_hpo_poll_timeout_seconds,
            fallback_extractor=deeprare_fallback,
        )
    elif cfg.phenotype_extractor.enabled and cfg.openai.api_key:
        phenotype_extractor = deeprare_fallback
    else:
        phenotype_extractor = narrative_fallback

    if cfg.knowledge_searcher.enabled and cfg.openai.api_key:
        try:
            knowledge_searcher = DeepRareKnowledgeSearcher(
                search_engine=cfg.knowledge_searcher.search_engine,
                google_api=cfg.knowledge_searcher.google_api,
                search_engine_id=cfg.knowledge_searcher.search_engine_id,
                chrome_driver=cfg.knowledge_searcher.chrome_driver,
                visualize=cfg.knowledge_searcher.visualize,
                openai_api_key=cfg.openai.api_key,
                openai_base_url=cfg.openai.base_url,
                mini_model_name=cfg.knowledge_searcher.mini_model_name,
                web_results=cfg.knowledge_searcher.web_results,
                pubmed_results=cfg.knowledge_searcher.pubmed_results,
                arxiv_results=cfg.knowledge_searcher.arxiv_results,
                wiki_results=cfg.knowledge_searcher.wiki_results,
            )
        except Exception:
            knowledge_searcher = StubKnowledgeSearcher()
    else:
        knowledge_searcher = StubKnowledgeSearcher()

    if cfg.case_searcher.enabled and cfg.case_searcher.mode == "fertility_dual":
        try:
            case_searcher = FertilityDualCaseSearcher(
                public_case_bank_path=cfg.case_searcher.public_case_bank_path,
                private_testing_case_bank_path=cfg.case_searcher.private_testing_case_bank_path,
                vector_index_path=cfg.case_searcher.vector_index_path,
                vector_metadata_path=cfg.case_searcher.vector_metadata_path,
                vectorizer_path=cfg.case_searcher.vectorizer_path,
                public_return_k=cfg.case_searcher.public_return_k,
                private_return_k=cfg.case_searcher.private_return_k,
                vector_top_n=cfg.case_searcher.vector_top_n,
                vector_weight=cfg.case_searcher.vector_weight,
                min_score=cfg.case_searcher.min_score,
            )
        except Exception:
            case_searcher = StubCaseSearcher()
    elif cfg.case_searcher.enabled and cfg.openai.api_key:
        try:
            case_searcher = DeepRareCaseSearcher(
                openai_api_key=cfg.openai.api_key,
                openai_base_url=cfg.openai.base_url,
                embedding_model=cfg.case_searcher.embedding_model,
                case_bank_path=cfg.case_searcher.case_bank_path,
                top_n=cfg.case_searcher.top_n,
                return_k=cfg.case_searcher.return_k,
                llm_filter=cfg.case_searcher.llm_filter,
                filter_model_name=cfg.case_searcher.filter_model_name,
            )
        except Exception:
            case_searcher = StubCaseSearcher()
    else:
        case_searcher = StubCaseSearcher()

    return ClinicalMvpPipeline(
        phenotype_extractor=phenotype_extractor,
        phenotype_analyser=(
            DeepRarePhenotypeAnalyser(
                chrome_driver=cfg.knowledge_searcher.chrome_driver,
                visualize=cfg.knowledge_searcher.visualize,
                enable_pubcasefinder=cfg.phenotype_analyser.enable_pubcasefinder,
                enable_phenobrain=cfg.phenotype_analyser.enable_phenobrain,
                enable_hpo_association=cfg.phenotype_analyser.enable_hpo_association,
                hpo_association_top_n=cfg.phenotype_analyser.hpo_association_top_n,
            )
            if cfg.openai.api_key and cfg.phenotype_analyser.enabled
            else StubPhenotypeAnalyser()
        ),
        knowledge_searcher=knowledge_searcher,
        case_searcher=case_searcher,
        initial_diagnosis_synthesizer=(
            LlmInitialDiagnosisSynthesizer(
                api_key=cfg.openai.api_key,
                base_url=cfg.openai.base_url,
                model_name=cfg.reasoning.model_name,
            )
            if cfg.openai.api_key
            else StubInitialDiagnosisSynthesizer()
        ),
        disease_normalizer=(
            LocalDiseaseNormalizer(
                api_key=cfg.openai.api_key,
                base_url=cfg.openai.base_url,
                model_name=cfg.reasoning.model_name,
                orpha_concept2id_path=cfg.reasoning.orpha_concept2id_path,
                orpha2name_path=cfg.reasoning.orpha2name_path,
                orpha2omim_path=cfg.reasoning.orpha2omim_path,
                top_n=cfg.reasoning.disease_normalization_top_n,
                llm_temperature=cfg.reasoning.disease_normalization_llm_temperature,
            )
            if cfg.openai.api_key
            else StubDiseaseNormalizer()
        ),
        per_disease_verifier=(
            LlmPerDiseaseVerifier(
                api_key=cfg.openai.api_key,
                base_url=cfg.openai.base_url,
                model_name=cfg.reasoning.model_name,
                orphanet_path=cfg.reasoning.orphanet_path,
            )
            if cfg.openai.api_key
            else StubPerDiseaseVerifier()
        ),
        final_diagnosis_synthesizer=(
            LlmFinalDiagnosisSynthesizer(
                api_key=cfg.openai.api_key,
                base_url=cfg.openai.base_url,
                model_name=cfg.reasoning.model_name,
            )
            if cfg.openai.api_key
            else StubFinalDiagnosisSynthesizer()
        ),
        stage_notes={
            "scope": "clinical-only MVP",
            "model": "DeepRare-style orchestration with real retrieval, local normalization, and LLM synthesis",
        },
    )
