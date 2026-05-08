from yk_ferta.agents.factory import build_clinical_mvp_pipeline
from yk_ferta.config import (
    CaseSearcherConfig,
    ClinicalMvpConfig,
    KnowledgeSearcherConfig,
    OpenAIConfig,
    PhenotypeExtractorConfig,
)
from yk_ferta.schemas.clinical import CandidateCondition, PatientProfile
from yk_ferta.schemas.clinical import PhenotypeItem
from yk_ferta.schemas.evidence import CandidateReview
from yk_ferta.schemas.mvp import ClinicalMvpRequest
from yk_ferta.schemas.mvp import NormalizedDisease, SimilarCase
from yk_ferta.services.clinical_mvp import (
    DeepRarePhenotypeAnalyser,
    DeepRareKnowledgeSearcher,
    FertilityDualCaseSearcher,
    LlmFinalDiagnosisSynthesizer,
    LlmInitialDiagnosisSynthesizer,
    LocalDiseaseNormalizer,
    LlmPerDiseaseVerifier,
    RagHpoPhenotypeExtractor,
    _patient_has_molecular_evidence,
    _soften_unconfirmed_molecular_candidate,
)


def _stub_config() -> ClinicalMvpConfig:
    return ClinicalMvpConfig(
        openai=OpenAIConfig(api_key="", base_url=""),
        phenotype_extractor=PhenotypeExtractorConfig(enabled=False),
        knowledge_searcher=KnowledgeSearcherConfig(enabled=False),
        case_searcher=CaseSearcherConfig(enabled=False),
    )


def test_clinical_mvp_pipeline_returns_deeprare_style_stage_outputs():
    pipeline = build_clinical_mvp_pipeline(config=_stub_config())
    request = ClinicalMvpRequest(
        patient=PatientProfile(
            patient_id="mvp-001",
            chief_complaint="Infertility for 2 years",
            history="Irregular cycles and prior endocrine evaluation.",
            laboratory_findings="Low AMH and elevated FSH.",
        ),
        top_k=3,
    )

    response = pipeline.run(request)

    assert response.patient_id == "mvp-001"
    assert len(response.phenotype_hints) == 1
    assert len(response.phenotype_tool_runs) == 1
    assert response.phenotype_tool_runs[0].status == "success"
    assert len(response.knowledge_evidence) == 1
    assert len(response.similar_cases) == 1
    assert len(response.initial_candidates) == 1
    assert len(response.normalized_candidates) == 1
    assert len(response.reviews) == 1
    assert response.final_recommendation.cautions
    assert response.final_recommendation.diagnosis_cards


def test_clinical_mvp_pipeline_respects_top_k_ceiling():
    pipeline = build_clinical_mvp_pipeline(config=_stub_config())
    request = ClinicalMvpRequest(
        patient=PatientProfile(patient_id="mvp-002", raw_note="Minimal narrative"),
        top_k=1,
    )

    response = pipeline.run(request)

    assert len(response.initial_candidates) <= 1


def test_rag_hpo_result_can_be_mapped_to_phenotype_items():
    extractor = RagHpoPhenotypeExtractor()
    result = {
        "task_id": "task-001",
        "case_id": "case-001",
        "status": "success",
        "result": {
            "persons": [
                {
                    "person_role": "self",
                    "phenotypes": [
                        {
                            "phenotype_name": "复发性葡萄胎",
                            "hpo_id": "HP:0032192",
                            "hpo_name": "Hydatidiform mole",
                            "chpo_name": "葡萄胎",
                            "similarity": 0.84,
                            "parse_reason": "LLM_recheck_match",
                        },
                        {
                            "phenotype_name": "复发性葡萄胎",
                            "hpo_id": "HP:0032192",
                            "hpo_name": "Hydatidiform mole",
                            "chpo_name": "葡萄胎",
                            "similarity": 0.80,
                            "parse_reason": "duplicate",
                        },
                    ],
                }
            ]
        },
    }

    phenotypes = extractor._result_to_phenotypes(result)

    assert len(phenotypes) == 1
    assert phenotypes[0].label == "Hydatidiform mole"
    assert phenotypes[0].chinese_label == "葡萄胎"
    assert phenotypes[0].code == "HP:0032192"
    assert phenotypes[0].source == "rag-hpo-service"


def test_factory_can_select_rag_hpo_extractor():
    config = ClinicalMvpConfig(
        openai=OpenAIConfig(api_key="", base_url=""),
        phenotype_extractor=PhenotypeExtractorConfig(
            enabled=True,
            provider="rag_hpo",
            rag_hpo_base_url="http://127.0.0.1:18080",
        ),
        knowledge_searcher=KnowledgeSearcherConfig(enabled=False),
        case_searcher=CaseSearcherConfig(enabled=False),
    )

    pipeline = build_clinical_mvp_pipeline(config=config)

    assert isinstance(pipeline.phenotype_extractor, RagHpoPhenotypeExtractor)


def test_knowledge_searcher_builds_fertility_focused_hydatidiform_mole_queries():
    searcher = DeepRareKnowledgeSearcher()
    queries = searcher._build_queries(
        PatientProfile(patient_id="case-001", raw_note="复发性葡萄胎，不孕。"),
        [
            PhenotypeItem(label="Infertility", code="HP:0000789"),
            PhenotypeItem(label="Hydatidiform mole", code="HP:0032192"),
        ],
    )

    assert "recurrent hydatidiform mole" in queries["web"]
    assert "NLRP7" not in queries["web"]
    assert "KHDC3L" not in queries["pubmed"]
    assert "infertility" in queries["pubmed"].lower()


def test_knowledge_searcher_scores_relevance_with_generic_query_overlap():
    searcher = DeepRareKnowledgeSearcher()

    strong_score = searcher._score_pubmed_article(
        '"recurrent hydatidiform mole" AND infertility',
        "Recurrent hydatidiform mole in women with infertility",
        "This review discusses recurrent hydatidiform mole and infertility.",
    )
    weak_score = searcher._score_pubmed_article(
        '"recurrent hydatidiform mole" AND infertility',
        "Adult granulosa cell tumor",
        "This case report focuses on ovarian tumor pathology.",
    )

    assert 0.0 <= weak_score <= 1.0
    assert 0.0 <= strong_score <= 1.0
    assert strong_score > weak_score
    assert strong_score >= 0.7


def test_append_evidence_can_carry_relevance_score():
    searcher = DeepRareKnowledgeSearcher()
    evidence = []

    searcher._append_evidence(
        evidence,
        "web-001",
        "web_search",
        "Web search results",
        "Hydatidiform mole and infertility background.",
        relevance_score=0.82,
    )

    assert len(evidence) == 1
    assert evidence[0].relevance_score == 0.82


def test_knowledge_searcher_does_not_append_failure_as_evidence():
    searcher = DeepRareKnowledgeSearcher()
    evidence = []

    searcher._append_evidence(
        evidence,
        "web-search-error",
        "web_search_error",
        "Web search error",
        "Web search failed: no results found",
    )

    assert evidence == []


def test_fertility_dual_case_searcher_separates_public_and_private_roles(tmp_path):
    import pandas as pd

    public_path = tmp_path / "public.csv"
    private_path = tmp_path / "private.csv"
    pd.DataFrame(
        [
            {
                "_id": "public-1",
                "case_report": "Recurrent hydatidiform mole and infertility.",
                "diagnosis": "Recurrent hydatidiform mole",
                "Orpha_name": "Recurrent hydatidiform mole",
                "Orpha_id": "999999",
                "matched_terms": "hydatidiform mole|infertility",
                "matched_categories": "molar_pregnancy|infertility",
                "fertility_relevance_tier": "strong",
                "source_dataset": "demo",
                "source_record_id": "demo-1",
            }
        ]
    ).to_csv(public_path, index=False)
    pd.DataFrame(
        [
            {
                "_id": "private-1",
                "case_report": "Clinical information: 复发性葡萄胎. Genetic findings: NLRP7 variant.",
                "diagnosis": "No final diagnosis; phenotype-matched variants in NLRP7",
                "clinical_suspected_diagnosis": "",
                "hpo_labels": "Hydatidiform mole",
                "hpo_terms": "HP:0032192",
                "retrieval_tags": "molar_pregnancy|phenotype_relevant_variant",
                "reported_genes": "NLRP7",
                "phenotype_relevant_genes": "NLRP7",
                "variant_summary": "NLRP7; variant",
                "phenotype_relevant_variant_count": 1,
                "data_quality": "high",
                "project_id": "P1",
                "test_project": "GDT",
                "report_status": "positive",
            }
        ]
    ).to_csv(private_path, index=False)

    searcher = FertilityDualCaseSearcher(
        public_case_bank_path=str(public_path),
        private_testing_case_bank_path=str(private_path),
        public_return_k=1,
        private_return_k=1,
    )
    results = searcher.search(
        PatientProfile(patient_id="case-1", raw_note="复发性葡萄胎，NLRP7相关风险"),
        [PhenotypeItem(label="Hydatidiform mole", code="HP:0032192")],
    )

    assert [item.evidence_role for item in results] == [
        "diagnosis_reference",
        "testing_finding_reference",
    ]
    assert results[0].disease_id == "ORPHA:999999"
    assert results[0].metadata["source_pmid"] == ""
    assert results[1].phenotype_relevant_genes == ["NLRP7"]


def test_per_disease_verifier_collects_candidate_level_orphanet_evidence(tmp_path):
    orphanet_path = tmp_path / "orphanet.json"
    orphanet_path.write_text(
        """
        {
          "ORPHA:1": {
            "name": "Demo fertility condition",
            "expert_link": "https://example.org/orpha/1",
            "hpo_associations": [
              ["Infertility", "HP:0000789", "Frequent"]
            ]
          }
        }
        """,
        encoding="utf-8",
    )
    verifier = LlmPerDiseaseVerifier(
        api_key="",
        model_name="",
        orphanet_path=str(orphanet_path),
        candidate_pubmed_results=0,
    )

    reviews = verifier.verify(
        PatientProfile(patient_id="case-1", raw_note="Infertility."),
        [PhenotypeItem(label="Infertility", code="HP:0000789")],
        [],
        [],
        [
            NormalizedDisease(
                original_name="Demo fertility condition",
                normalized_name="Demo fertility condition",
                disease_id="ORPHA:1",
                ontology="Orphanet",
                mapping_score=1.0,
            )
        ],
    )

    assert reviews[0].is_supported is True
    assert reviews[0].supporting_evidence == ["candidate-orphanet-orpha-1"]
    assert verifier.last_candidate_evidence[0].source_type == "orphanet"


def test_final_synthesizer_no_api_returns_structured_diagnosis_cards():
    synthesizer = LlmFinalDiagnosisSynthesizer(api_key="", model_name="")

    recommendation = synthesizer.synthesize(
        PatientProfile(patient_id="case-1", raw_note="Two prior hydatidiform mole pregnancies."),
        [PhenotypeItem(label="Hydatidiform mole", code="HP:0032192")],
        [],
        [],
        [],
        [
            CandidateCondition(
                name="Recurrent hydatidiform mole due to NLRP7 mutations",
                rank=1,
                score=0.9,
                rationale="Phenotype match.",
            )
        ],
        [],
        [
            CandidateReview(
                candidate_name="Recurrent hydatidiform mole due to NLRP7 mutations",
                is_supported=True,
                confidence=0.7,
                supporting_evidence=["candidate-orphanet-demo"],
                missing_evidence=["No molecular result."],
            )
        ],
    )

    assert recommendation.diagnosis_cards
    card = recommendation.diagnosis_cards[0]
    assert "rank" in card
    assert "diagnosis_match_score" in card
    assert "diagnosis_match_percent" in card
    assert "disease_name_zh" in card
    assert "disease_name_en" in card
    assert card["clinical_diagnosis"] == "Recurrent hydatidiform mole due to NLRP7 mutations"
    assert "possible_molecular_subtype" not in card
    assert "molecular_mechanism" in card
    assert card["missing_evidence"]
    assert recommendation.final_diagnosis_confidence_percent == int(round(card["confidence"] * 100))


def test_final_synthesizer_no_api_keeps_molecular_info_disease_scoped():
    synthesizer = LlmFinalDiagnosisSynthesizer(api_key="", model_name="")

    recommendation = synthesizer.synthesize(
        PatientProfile(patient_id="case-xxxyy", raw_note="Infertility and developmental delay."),
        [PhenotypeItem(label="Infertility", code="HP:0000789")],
        [],
        [],
        [],
        [
            CandidateCondition(
                name="48,XXYY syndrome",
                rank=1,
                score=0.9,
                rationale="Compatible phenotype.",
            ),
            CandidateCondition(
                name="Fragile X syndrome",
                rank=2,
                score=0.4,
                rationale="Differential diagnosis.",
            ),
        ],
        [],
        [],
    )

    card = recommendation.diagnosis_cards[0]
    assert card["clinical_diagnosis"] == "48,XXYY syndrome"
    assert "possible_molecular_subtype" not in card
    assert "Fragile X" not in str(card.get("molecular_mechanism", ""))


def test_final_synthesizer_ignores_llm_hallucinated_omim_id():
    class FakeReasoner:
        def complete(self, system_prompt, user_prompt, *, temperature=None, seed=42):
            return (
                '{"summary":"...","diagnosis_cards":[{'
                '"disease_name_zh":"克氏综合征",'
                '"disease_name_en":"Klinefelter syndrome",'
                '"clinical_diagnosis":"Klinefelter综合征",'
                '"support_level":"高",'
                '"confidence":0.95,'
                '"omim_id":"300000",'
                '"omim_url":"https://www.omim.org/entry/300000",'
                '"orphanet_id":"ORPHA:484",'
                '"orphanet_url":"https://www.orpha.net/consor/cgi-bin/OC_Exp.php?Expert=484",'
                '"inheritance":"NA",'
                '"disease_genes":[],'
                '"molecular_mechanism":"NA",'
                '"pathogenesis":"NA",'
                '"specialties":["遗传科"],'
                '"supporting_evidence":["支持"],'
                '"contradicting_evidence":[],'
                '"missing_evidence":[],'
                '"recommended_tests":[],'
                '"references":[],'
                '"cautions":[]'
                '}],'
                '"next_steps":["..."],'
                '"cautions":[]}'
            )

    synthesizer = LlmFinalDiagnosisSynthesizer(api_key="demo", model_name="demo")
    synthesizer._reasoner = FakeReasoner()

    recommendation = synthesizer.synthesize(
        patient=PatientProfile(patient_id="case-kf", raw_note="不育，考虑克氏综合征。"),
        phenotypes=[PhenotypeItem(label="Infertility", code="HP:0000789")],
        phenotype_hints=[],
        knowledge_evidence=[],
        similar_cases=[],
        initial_candidates=[
            CandidateCondition(
                name="Klinefelter综合征",
                rank=1,
                score=0.95,
                rationale="Compatible phenotype.",
            )
        ],
        normalized_candidates=[
            NormalizedDisease(
                original_name="Klinefelter综合征",
                normalized_name="NON RARE IN EUROPE: Klinefelter syndrome",
                disease_id="ORPHA:484",
                ontology="Orphanet",
            )
        ],
        reviews=[CandidateReview("Klinefelter综合征", True, confidence=0.95)],
    )

    card = recommendation.diagnosis_cards[0]
    assert card["omim_id"] == "NA"
    assert card["omim_url"] == ""
    assert card["orphanet_id"] == "ORPHA:484"


def test_final_synthesizer_uses_canonical_ranking_not_llm_card_order():
    class FakeReasoner:
        def complete(self, system_prompt, user_prompt, *, temperature=None, seed=42):
            return (
                '{"summary":"...","diagnosis_cards":['
                '{'
                '"disease_name_zh":"妊娠滋养细胞疾病",'
                '"disease_name_en":"Gestational trophoblastic disease",'
                '"clinical_diagnosis":"Gestational trophoblastic disease",'
                '"support_level":"高",'
                '"confidence":0.95,'
                '"inheritance":"NA",'
                '"disease_genes":[],'
                '"molecular_mechanism":"NA",'
                '"pathogenesis":"NA",'
                '"specialties":["妇科"],'
                '"supporting_evidence":["广义疾病谱支持"],'
                '"contradicting_evidence":[],'
                '"missing_evidence":[],'
                '"recommended_tests":[],'
                '"references":[],'
                '"cautions":[]'
                '},'
                '{'
                '"disease_name_zh":"家族性复发性葡萄胎",'
                '"disease_name_en":"Familial recurrent hydatidiform mole",'
                '"clinical_diagnosis":"Familial recurrent hydatidiform mole",'
                '"support_level":"中",'
                '"confidence":0.70,'
                '"inheritance":"常染色体隐性遗传",'
                '"disease_genes":["NLRP7","KHDC3L"],'
                '"molecular_mechanism":"NA",'
                '"pathogenesis":"NA",'
                '"specialties":["生殖遗传"],'
                '"supporting_evidence":["表型高度吻合"],'
                '"contradicting_evidence":[],'
                '"missing_evidence":["待基因检测"],'
                '"recommended_tests":["基因检测"],'
                '"references":[],'
                '"cautions":[]'
                '}],'
                '"next_steps":["..."],'
                '"cautions":[]}'
            )

    synthesizer = LlmFinalDiagnosisSynthesizer(api_key="demo", model_name="demo")
    synthesizer._reasoner = FakeReasoner()

    recommendation = synthesizer.synthesize(
        patient=PatientProfile(patient_id="case-frhm", raw_note="两次葡萄胎妊娠，不孕。"),
        phenotypes=[
            PhenotypeItem(label="Hydatidiform mole", code="HP:0032192"),
            PhenotypeItem(label="Infertility", code="HP:0000789"),
        ],
        phenotype_hints=[],
        knowledge_evidence=[],
        similar_cases=[],
        initial_candidates=[
            CandidateCondition(
                name="Familial recurrent hydatidiform mole",
                rank=1,
                score=0.95,
                rationale="Direct phenotype fit.",
            ),
            CandidateCondition(
                name="Gestational trophoblastic disease",
                rank=2,
                score=0.55,
                rationale="Broader disease spectrum.",
            ),
        ],
        normalized_candidates=[],
        reviews=[
            CandidateReview("Familial recurrent hydatidiform mole", True, confidence=0.90),
            CandidateReview("Gestational trophoblastic disease", True, confidence=0.60),
        ],
    )

    assert recommendation.diagnosis_cards[0]["clinical_diagnosis"] == "Familial recurrent hydatidiform mole"
    assert recommendation.diagnosis_cards[0]["rank"] == 1
    assert recommendation.diagnosis_cards[0]["diagnosis_match_percent"] >= recommendation.diagnosis_cards[1]["diagnosis_match_percent"]


def test_final_synthesizer_localizes_english_diagnosis_titles_to_chinese():
    class FakeReasoner:
        def complete(self, system_prompt, user_prompt, *, temperature=None, seed=42):
            if "整理疾病展示名" in system_prompt:
                return (
                    '{"cards":['
                    '{"index":0,"disease_name_zh":"家族性复发性葡萄胎","disease_name_en":"Hydatidiform mole"},'
                    '{"index":1,"disease_name_zh":"复发性妊娠滋养细胞疾病","disease_name_en":"Gestational trophoblastic disease"}'
                    ']}'
                )
            return (
                '{"summary":"...","diagnosis_cards":['
                '{'
                '"disease_name_zh":"Recurrent Hydatidiform Mole, Autosomal Recessive",'
                '"disease_name_en":"Hydatidiform mole",'
                '"clinical_diagnosis":"Recurrent Hydatidiform Mole, Autosomal Recessive",'
                '"support_level":"中",'
                '"confidence":0.85,'
                '"inheritance":"常染色体隐性遗传",'
                '"disease_genes":["NLRP7","KHDC3L"],'
                '"molecular_mechanism":"NA",'
                '"pathogenesis":"NA",'
                '"specialties":["生殖遗传"],'
                '"supporting_evidence":["表型高度吻合"],'
                '"contradicting_evidence":[],'
                '"missing_evidence":["待基因检测"],'
                '"recommended_tests":["基因检测"],'
                '"references":[],'
                '"cautions":[]'
                '},'
                '{'
                '"disease_name_zh":"Gestational Trophoblastic Disease, Recurrent",'
                '"disease_name_en":"Gestational trophoblastic disease",'
                '"clinical_diagnosis":"Gestational trophoblastic disease",'
                '"support_level":"中",'
                '"confidence":0.75,'
                '"inheritance":"NA",'
                '"disease_genes":[],'
                '"molecular_mechanism":"NA",'
                '"pathogenesis":"NA",'
                '"specialties":["妇科"],'
                '"supporting_evidence":["广义疾病谱支持"],'
                '"contradicting_evidence":[],'
                '"missing_evidence":[],'
                '"recommended_tests":[],'
                '"references":[],'
                '"cautions":[]'
                '}],'
                '"next_steps":["..."],'
                '"cautions":[]}'
            )

    synthesizer = LlmFinalDiagnosisSynthesizer(api_key="demo", model_name="demo")
    synthesizer._reasoner = FakeReasoner()

    recommendation = synthesizer.synthesize(
        patient=PatientProfile(patient_id="case-zh-name", raw_note="两次葡萄胎妊娠，不孕。"),
        phenotypes=[
            PhenotypeItem(label="Hydatidiform mole", code="HP:0032192"),
            PhenotypeItem(label="Infertility", code="HP:0000789"),
        ],
        phenotype_hints=[],
        knowledge_evidence=[],
        similar_cases=[],
        initial_candidates=[
            CandidateCondition(
                name="Recurrent Hydatidiform Mole, Autosomal Recessive",
                rank=1,
                score=0.95,
                rationale="Direct phenotype fit.",
            ),
            CandidateCondition(
                name="Gestational Trophoblastic Disease, Recurrent",
                rank=2,
                score=0.60,
                rationale="Broader disease spectrum.",
            ),
        ],
        normalized_candidates=[
            NormalizedDisease(
                original_name="Recurrent Hydatidiform Mole, Autosomal Recessive",
                normalized_name="Hydatidiform mole",
                disease_id="ORPHA:99927",
                ontology="Orphanet/OMIM:OMIM:231090",
                normalization_decision_confidence=0.85,
            ),
            NormalizedDisease(
                original_name="Gestational Trophoblastic Disease, Recurrent",
                normalized_name="Gestational trophoblastic disease",
                disease_id="ORPHA:254685",
                ontology="Orphanet",
                normalization_decision_confidence=0.74,
            ),
        ],
        reviews=[
            CandidateReview("Hydatidiform mole", True, confidence=0.85),
            CandidateReview("Gestational trophoblastic disease", True, confidence=0.75),
        ],
    )

    assert recommendation.diagnosis_cards[0]["disease_name_zh"] == "家族性复发性葡萄胎"
    assert recommendation.diagnosis_cards[0]["disease_name_en"] == "Hydatidiform mole"
    assert recommendation.diagnosis_cards[1]["disease_name_zh"] == "复发性妊娠滋养细胞疾病"


def test_final_synthesizer_localizes_fallback_cards_when_final_json_is_invalid():
    class FakeReasoner:
        def complete(self, system_prompt, user_prompt, *, temperature=None, seed=42):
            if "整理疾病展示名" in system_prompt:
                return (
                    '{"cards":['
                    '{"index":0,"disease_name_zh":"复发性葡萄胎","disease_name_en":"Hydatidiform mole"}'
                    ']}'
                )
            if "整理诊断卡展示内容" in system_prompt:
                return (
                    '{"cards":['
                    '{'
                    '"index":0,'
                    '"clinical_diagnosis":"复发性葡萄胎",'
                    '"inheritance":"常染色体隐性遗传",'
                    '"molecular_mechanism":"NLRP7 / KHDC3L 相关印记异常，待患者本人检测确认。",'
                    '"pathogenesis":"与母体效应基因相关的葡萄胎形成机制待进一步确认。",'
                    '"specialties":["生殖遗传"],'
                    '"supporting_evidence":["表型与葡萄胎高度吻合"],'
                    '"contradicting_evidence":[],'
                    '"missing_evidence":["需补充患者本人的基因检测结果"],'
                    '"recommended_tests":["建议进行相关基因检测"]'
                    '}]}'
                )
            return "Request timed out."

    synthesizer = LlmFinalDiagnosisSynthesizer(api_key="demo", model_name="demo")
    synthesizer._reasoner = FakeReasoner()

    recommendation = synthesizer.synthesize(
        patient=PatientProfile(patient_id="case-fallback-zh", raw_note="两次葡萄胎妊娠，不孕。"),
        phenotypes=[PhenotypeItem(label="Hydatidiform mole", code="HP:0032192")],
        phenotype_hints=[],
        knowledge_evidence=[],
        similar_cases=[],
        initial_candidates=[
            CandidateCondition(
                name="Recurrent Hydatidiform Mole",
                rank=1,
                score=0.95,
                rationale="Direct phenotype fit.",
            )
        ],
        normalized_candidates=[
            NormalizedDisease(
                original_name="Recurrent Hydatidiform Mole",
                normalized_name="Hydatidiform mole",
                disease_id="ORPHA:99927",
                ontology="Orphanet/OMIM:OMIM:231090",
                normalization_decision_confidence=0.77,
            )
        ],
        reviews=[CandidateReview("Hydatidiform mole", True, confidence=0.85)],
    )

    assert recommendation.summary.startswith("Clinical MVP 已按 DeepRare 风格完成一轮可追溯鉴别诊断")
    assert recommendation.diagnosis_cards[0]["disease_name_zh"] == "复发性葡萄胎"
    assert recommendation.diagnosis_cards[0]["disease_name_en"] == "Hydatidiform mole"
    assert recommendation.diagnosis_cards[0]["clinical_diagnosis"] == "复发性葡萄胎"
    assert recommendation.diagnosis_cards[0]["supporting_evidence"] == ["表型与葡萄胎高度吻合"]
    assert recommendation.diagnosis_cards[0]["recommended_tests"] == ["建议进行相关基因检测"]


def test_per_disease_verifier_localizes_review_display_fields_to_chinese():
    class FakeReasoner:
        def __init__(self):
            self.calls = 0

        def complete(self, system_prompt, user_prompt, *, temperature=None, seed=42):
            self.calls += 1
            if "整理候选疾病复核结果" in system_prompt:
                return (
                    '{"reviews":['
                    '{'
                    '"index":0,'
                    '"reasoning":"结合当前表型与候选疾病知识，临床上支持该诊断，但仍缺少患者本人分子证据。",'
                    '"supporting_evidence":["高前额（HP:0000294）与该综合征典型表型一致"],'
                    '"contradicting_evidence":[],'
                    '"missing_evidence":["需补充患者本人的基因检测结果"]'
                    '}]}'
                )
            return (
                '{'
                '"is_supported": true,'
                '"confidence": 0.85,'
                '"reasoning":"High forehead matches the syndrome pattern.",'
                '"evidence_ids":["candidate-orphanet-demo"],'
                '"supporting_evidence":["High forehead matches the syndrome pattern."],'
                '"contradicting_evidence":[],'
                '"missing_evidence":["No molecular confirmation yet."]'
                '}'
            )

    verifier = LlmPerDiseaseVerifier(api_key="demo", model_name="demo")
    verifier._orphanet = {
        "ORPHA:199": {
            "name": "Cornelia de Lange syndrome",
            "expert_link": "https://example.org/orpha199",
            "hpo_associations": [["High forehead", "HP:0000294", "Frequent"]],
        }
    }
    verifier._reasoner = FakeReasoner()

    reviews = verifier.verify(
        patient=PatientProfile(patient_id="case-review-zh", raw_note="高前额，喂养困难。"),
        phenotypes=[PhenotypeItem(label="High forehead", code="HP:0000294")],
        similar_cases=[],
        knowledge_evidence=[],
        normalized_candidates=[
            NormalizedDisease(
                original_name="Cornelia de Lange syndrome",
                normalized_name="Cornelia de Lange syndrome",
                disease_id="ORPHA:199",
                ontology="Orphanet",
            )
        ],
    )

    assert reviews[0].reasoning.startswith("结合当前表型与候选疾病知识")
    assert reviews[0].supporting_evidence == ["高前额（HP:0000294）与该综合征典型表型一致"]
    assert reviews[0].missing_evidence == ["需补充患者本人的基因检测结果"]


def test_unconfirmed_molecular_candidate_name_is_softened_without_patient_variant():
    synthesizer = LlmInitialDiagnosisSynthesizer(api_key="", model_name="")

    candidates = synthesizer._parse_candidates(
        {
            "candidates": [
                {
                    "name": "Recurrent Hydatidiform Mole due to NLRP7 mutations (HYDM1)",
                    "score": 0.9,
                    "rationale": "The phenotype suggests familial recurrent hydatidiform mole.",
                    "supporting_phenotypes": ["Hydatidiform mole"],
                }
            ]
        },
        top_k=1,
        phenotypes=[PhenotypeItem(label="Hydatidiform mole", code="HP:0032192")],
        has_patient_molecular_evidence=False,
    )

    assert candidates[0].name == "Recurrent Hydatidiform Mole（分子病因未确认）"
    assert "未提供患者本人的基因/变异检测结果" in candidates[0].rationale


def test_patient_molecular_evidence_detection_requires_variant_result_context():
    assert not _patient_has_molecular_evidence(
        PatientProfile(
            patient_id="case-no-variant",
            raw_note="复发性葡萄胎，建议进行NLRP7和KHDC3L基因检测。",
        )
    )
    assert _patient_has_molecular_evidence(
        PatientProfile(
            patient_id="case-with-variant",
            raw_note="WES检测检出NLRP7致病变异，c.123A>T。",
        )
    )


def test_phenobrain_ranked_text_parser_preserves_comma_containing_names():
    analyser = DeepRarePhenotypeAnalyser()
    parsed = analyser._parse_ranked_text(
        "phenobrain",
        (
            "Phenobrain gives related diseases about the patient: "
            "HYDATIDIFORM MOLE, RECURRENT, 2; HYDM2 (OMIM:614293), "
            "HYDATIDIFORM MOLE, RECURRENT, 3; HYDM3 (OMIM:618431), "
            "SPERMATOGENIC FAILURE 3; SPGF3 (OMIM:606766)"
        ),
    )

    assert parsed[0].disease_name == "HYDATIDIFORM MOLE, RECURRENT, 2; HYDM2"
    assert parsed[0].disease_id == "OMIM:614293"
    assert parsed[1].disease_name == "HYDATIDIFORM MOLE, RECURRENT, 3; HYDM3"
    assert all(item.disease_name != "RECURRENT" for item in parsed)


def test_hpo_association_uses_hpo_api_rows(monkeypatch):
    class DummyResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "diseases": [
                    {"id": "OMIM:231090", "name": "Hydatidiform mole, recurrent, 1"},
                    {"id": "OMIM:618431", "name": "Hydatidiform mole, recurrent, 3"},
                ]
            }

    def fake_get(url, headers, timeout):
        assert "HP:0032192" in url
        assert headers["accept"] == "application/json"
        assert timeout == 20
        return DummyResponse()

    monkeypatch.setattr("tools.hpo_search.requests.get", fake_get)

    analyser = DeepRarePhenotypeAnalyser(
        enable_pubcasefinder=False,
        enable_phenobrain=False,
        enable_hpo_association=True,
    )
    hits, tool_runs = analyser.analyze_with_details(
        PatientProfile(patient_id="case-1", raw_note="两次葡萄胎，不孕。"),
        [PhenotypeItem(label="Hydatidiform mole", code="HP:0032192")],
    )

    assert tool_runs[2].source == "hpo_association"
    assert tool_runs[2].status == "success"
    assert hits[0].source == "hpo_association"
    assert hits[0].disease_id == "OMIM:231090"
    assert hits[0].disease_name == "Hydatidiform mole, recurrent, 1"


def test_hpo_association_keeps_partial_success_when_one_hpo_fails(monkeypatch):
    def fake_hpo_search(_args, query):
        if query == "HP:0000789":
            raise RuntimeError("transient ssl issue")
        if query == "HP:0032192":
            return [
                "OMIM:231090 Hydatidiform mole, recurrent, 1",
                "OMIM:618431 Hydatidiform mole, recurrent, 3",
            ]
        return []

    monkeypatch.setattr("tools.hpo_search.HPOSearchTool", fake_hpo_search)

    analyser = DeepRarePhenotypeAnalyser(
        enable_pubcasefinder=False,
        enable_phenobrain=False,
        enable_hpo_association=True,
    )
    hits, tool_runs = analyser.analyze_with_details(
        PatientProfile(patient_id="case-1", raw_note="不孕，复发性葡萄胎。"),
        [
            PhenotypeItem(label="Infertility", code="HP:0000789"),
            PhenotypeItem(label="Hydatidiform mole", code="HP:0032192"),
        ],
    )

    assert tool_runs[2].source == "hpo_association"
    assert tool_runs[2].status == "success"
    assert "Partial failures:" in tool_runs[2].raw_result
    assert "HP:0000789: RuntimeError: transient ssl issue" in tool_runs[2].raw_result
    assert hits[0].disease_id == "OMIM:231090"
    assert hits[0].source == "hpo_association"


def test_hpo_association_respects_top_n_limit():
    analyser = DeepRarePhenotypeAnalyser(hpo_association_top_n=3)
    rows = [
        "ORPHA:1 Disease A",
        "ORPHA:2 Disease B",
        "ORPHA:3 Disease C",
        "ORPHA:4 Disease D",
        "ORPHA:5 Disease E",
    ]

    hits = analyser._parse_hpo_rows(rows)

    assert [item.disease_id for item in hits] == ["ORPHA:1", "ORPHA:2", "ORPHA:3"]


def test_soften_unconfirmed_molecular_candidate_prefers_clinical_name():
    name, rationale = _soften_unconfirmed_molecular_candidate(
        "NLRP7-related reproductive wastage syndrome",
        "Candidate fits phenotype.",
        has_patient_molecular_evidence=False,
    )

    assert name == "reproductive wastage syndrome（分子病因未确认）"
    assert "当前病例未提供患者本人的基因/变异检测结果" in rationale


def test_initial_diagnosis_prompt_hides_private_case_gene_framing(monkeypatch):
    captured = []

    class FakeReasoner:
        def complete(self, system_prompt, user_prompt):
            captured.append((system_prompt, user_prompt))
            return '{"candidates":[{"name":"Recurrent hydatidiform mole syndrome","score":0.9,"rationale":"符合表型。","supporting_phenotypes":["不孕不育","复发性葡萄胎妊娠"]}]}'

    synthesizer = LlmInitialDiagnosisSynthesizer(api_key="demo", model_name="demo")
    synthesizer._reasoner = FakeReasoner()

    similar_cases = [
        SimilarCase(
            case_id="private-1",
            source="private-testing-case-bank:hybrid",
            summary="Clinical information: 复发性葡萄胎. Genetic findings: NLRP7 variant.",
            diagnosis="No final diagnosis; phenotype-matched variants in NLRP7",
            score=0.88,
            evidence_role="testing_finding_reference",
            reported_genes=["NLRP7"],
            phenotype_relevant_genes=["NLRP7"],
            metadata={"hpo_labels": "Hydatidiform mole|Infertility", "retrieval_tags": "molar_pregnancy|phenotype_relevant_variant"},
        )
    ]

    candidates = synthesizer.synthesize(
        patient=PatientProfile(
            patient_id="case-1",
            chief_complaint="不孕不育",
            present_illness="既往两次葡萄胎妊娠",
            history="无基因检测结果",
        ),
        phenotypes=[
            PhenotypeItem(label="Infertility", code="HP:0000789"),
            PhenotypeItem(label="Hydatidiform mole", code="HP:0032192"),
        ],
        phenotype_hints=[],
        knowledge_evidence=[],
        similar_cases=similar_cases,
        top_k=2,
    )

    assert candidates[0].name == "Recurrent hydatidiform mole syndrome"
    evidence_prompt = captured[1][1]
    assert "genes=NLRP7" not in evidence_prompt
    assert "phenotype-matched variants in NLRP7" not in evidence_prompt
    assert "私有历史检测参考，无最终临床诊断" in evidence_prompt
    assert "本阶段目标：生成第一轮鉴别诊断候选" in evidence_prompt
    assert "不要使用过于宽泛的上位概念" in evidence_prompt


def test_final_synthesis_sorts_candidates_by_normalized_review_mapping():
    synthesizer = LlmFinalDiagnosisSynthesizer(api_key="", model_name="")
    candidate = CandidateCondition(
        name="Recurrent hydatidiform mole",
        rank=1,
        score=0.95,
        rationale="Two molar pregnancies.",
    )
    competing = CandidateCondition(
        name="Biparental complete hydatidiform mole (molecular etiology unconfirmed)",
        rank=2,
        score=0.85,
        rationale="Possible molecular etiology.",
    )

    recommendation = synthesizer.synthesize(
        patient=PatientProfile(patient_id="case-1", raw_note="两次葡萄胎，不孕。"),
        phenotypes=[PhenotypeItem(label="Hydatidiform mole", code="HP:0032192")],
        phenotype_hints=[],
        knowledge_evidence=[],
        similar_cases=[],
        initial_candidates=[competing, candidate],
        normalized_candidates=[
            NormalizedDisease(
                original_name="Recurrent hydatidiform mole",
                normalized_name="Hydatidiform mole",
                disease_id="ORPHA:99927",
                ontology="Orphanet",
            ),
            NormalizedDisease(
                original_name="Biparental complete hydatidiform mole (molecular etiology unconfirmed)",
                normalized_name="Biparental complete hydatidiform mole (molecular etiology unconfirmed)",
                ontology="unmapped",
            ),
        ],
        reviews=[
            CandidateReview("Hydatidiform mole", True, confidence=0.9),
            CandidateReview(
                "Biparental complete hydatidiform mole (molecular etiology unconfirmed)",
                True,
                confidence=0.75,
            ),
        ],
    )

    assert recommendation.candidates[0].name == "Recurrent hydatidiform mole"
    assert recommendation.diagnosis_cards[0]["clinical_diagnosis"] == "Recurrent hydatidiform mole"
    assert recommendation.diagnosis_cards[0]["confidence"] == 0.9
    assert "复核置信度=0.90" in recommendation.diagnosis_cards[0]["ranking_reason"]


def test_local_disease_normalizer_uses_deeprare_style_top1_recall(monkeypatch):
    import torch

    class DummyNormalizer(LocalDiseaseNormalizer):
        def _ensure_loaded(self):
            return None

        def _ensure_embeddings(self):
            return None

        def _encode_queries(self, _queries):
            return torch.tensor([[0.95, 0.05]], dtype=torch.float32)

        def _adjudicate_top_matches(self, disease_name, matches):
            return matches[0], "embedding_top1_fallback", "test fallback", None

    normalizer = DummyNormalizer()
    normalizer._concept2id = {
        "Hydatidiform mole": "ORPHA:99927",
        "Gestational trophoblastic disease": "ORPHA:254685",
    }
    normalizer._orpha2name = {
        "ORPHA:99927": "Hydatidiform mole",
        "ORPHA:254685": "Gestational trophoblastic disease",
    }
    normalizer._orpha2omim = {"ORPHA:99927": "OMIM:231090"}
    normalizer._concept_names = list(normalizer._concept2id.keys())
    normalizer._concept_ids = list(normalizer._concept2id.values())
    normalizer._concept_embeddings = torch.tensor(
        [
            [1.0, 0.0],
            [0.0, 1.0],
        ],
        dtype=torch.float32,
    )

    normalized = normalizer.normalize(
        [CandidateCondition(name="Recurrent Hydatidiform Mole", rank=1, score=0.9)]
    )

    assert normalized[0].disease_id == "ORPHA:99927"
    assert normalized[0].normalized_name == "Hydatidiform mole"
    assert normalized[0].ontology == "Orphanet/OMIM:OMIM:231090"
    assert normalized[0].mapping_score > 0.9
    assert normalized[0].normalization_decision_source == "embedding_top1_fallback"
    assert len(normalized[0].normalization_top_matches) == 2


def test_local_disease_normalizer_returns_unmapped_on_embedding_failure(monkeypatch):
    class FailingNormalizer(LocalDiseaseNormalizer):
        def _ensure_loaded(self):
            return None

        def _lookup_top_matches(self, _name):
            raise RuntimeError("embed fail")

    normalizer = FailingNormalizer()
    normalizer._concept2id = {"Hydatidiform mole": "ORPHA:99927"}
    normalizer._orpha2name = {"ORPHA:99927": "Hydatidiform mole"}
    normalizer._orpha2omim = {}
    normalizer._concept_names = list(normalizer._concept2id.keys())
    normalizer._concept_ids = list(normalizer._concept2id.values())

    normalized = normalizer.normalize(
        [CandidateCondition(name="Unknown candidate", rank=1, score=0.5)]
    )

    assert normalized[0].disease_id is None
    assert normalized[0].ontology == "unmapped"


def test_local_disease_normalizer_llm_can_select_non_top1_candidate():
    import torch

    class FakeReasoner:
        def complete(self, system_prompt, user_prompt, *, temperature=None, seed=42):
            assert temperature == 0.0
            assert seed == 42
            assert "原始候选病名：Biparental Complete Hydatidiform Mole" in user_prompt
            return (
                '{"decision":"select_candidate","selected_rank":2,'
                '"confidence":0.91,"reason":"候选 2 保留了 complete hydatidiform mole 的核心语义。"}'
            )

    class DummyNormalizer(LocalDiseaseNormalizer):
        def _ensure_loaded(self):
            return None

        def _ensure_embeddings(self):
            return None

        def _encode_queries(self, _queries):
            return torch.tensor([[0.8, 0.7, 0.1]], dtype=torch.float32)

    normalizer = DummyNormalizer(api_key="demo", model_name="demo", llm_temperature=0.0)
    normalizer._reasoner = FakeReasoner()
    normalizer._concept2id = {
        "Partial hydatidiform mole": "ORPHA:254693",
        "Complete hydatidiform mole": "ORPHA:254688",
        "Gestational trophoblastic disease": "ORPHA:254685",
    }
    normalizer._orpha2name = {
        "ORPHA:254693": "Partial hydatidiform mole",
        "ORPHA:254688": "Complete hydatidiform mole",
        "ORPHA:254685": "Gestational trophoblastic disease",
    }
    normalizer._orpha2omim = {"ORPHA:254688": "OMIM:231090"}
    normalizer._concept_names = list(normalizer._concept2id.keys())
    normalizer._concept_ids = list(normalizer._concept2id.values())
    normalizer._concept_embeddings = torch.tensor(
        [
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, 0.0, 1.0],
        ],
        dtype=torch.float32,
    )

    normalized = normalizer.normalize(
        [CandidateCondition(name="Biparental Complete Hydatidiform Mole", rank=1, score=0.9)]
    )

    assert normalized[0].disease_id == "ORPHA:254688"
    assert normalized[0].normalized_name == "Complete hydatidiform mole"
    assert normalized[0].normalization_decision_source == "llm_topn_adjudication"
    assert normalized[0].normalization_decision_confidence == 0.91
    assert len(normalized[0].normalization_top_matches) == 3


def test_local_disease_normalizer_llm_can_reject_top_matches():
    import torch

    class FakeReasoner:
        def complete(self, system_prompt, user_prompt, *, temperature=None, seed=42):
            return (
                '{"decision":"unmapped","selected_rank":0,'
                '"confidence":0.76,"reason":"候选列表均未可靠保留原始疾病语义。"}'
            )

    class DummyNormalizer(LocalDiseaseNormalizer):
        def _ensure_loaded(self):
            return None

        def _ensure_embeddings(self):
            return None

        def _encode_queries(self, _queries):
            return torch.tensor([[0.9, 0.8]], dtype=torch.float32)

    normalizer = DummyNormalizer(api_key="demo", model_name="demo", llm_temperature=0.0)
    normalizer._reasoner = FakeReasoner()
    normalizer._concept2id = {
        "Rare female infertility due to gonadal dysgenesis": "ORPHA:399877",
        "Mild hyperphenylalaninemia": "ORPHA:79651",
    }
    normalizer._orpha2name = {
        "ORPHA:399877": "Rare female infertility due to gonadal dysgenesis",
        "ORPHA:79651": "Mild hyperphenylalaninemia",
    }
    normalizer._orpha2omim = {}
    normalizer._concept_names = list(normalizer._concept2id.keys())
    normalizer._concept_ids = list(normalizer._concept2id.values())
    normalizer._concept_embeddings = torch.tensor(
        [
            [1.0, 0.0],
            [0.0, 1.0],
        ],
        dtype=torch.float32,
    )

    normalized = normalizer.normalize(
        [CandidateCondition(name="Imprinting Disorder with Reproductive Phenotype", rank=1, score=0.55)]
    )

    assert normalized[0].disease_id is None
    assert normalized[0].ontology == "unmapped"
    assert normalized[0].normalization_decision_source == "llm_unmapped"
    assert len(normalized[0].normalization_top_matches) == 2
