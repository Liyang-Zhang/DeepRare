# yk-FERTA 本地工作流 LLM 调用清单（不含表型提取外部服务）

本文档回答两个问题：

1. 除了起始表型提取（你当前走 `rag_hpo` 外部服务）外，后续流程一共调用多少次 LLM？
2. 每次调用使用的 prompt 是什么？

> 范围说明：基于当前代码与当前本地配置（`config/clinical_mvp.json`）分析，不包含 `rag_hpo` 服务内部实现。

---

## 1. 调用总数（当前配置）

当前配置关键点：

- `reasoning.model_name = qwen3-max`
- `case_searcher.mode = fertility_dual`
- `case_searcher.llm_filter = false`
- `knowledge_searcher.arxiv_results = 0`
- `knowledge_searcher.wiki_results = 0`

在这组配置下，单个任务的 LLM 调用次数为：

`2 + N + 1 = N + 3`

其中：

- `2`：初诊综合（LlmInitialDiagnosisSynthesizer）固定两次
- `N`：逐病复核（LlmPerDiseaseVerifier）每个候选病一次
- `1`：最终综合（LlmFinalDiagnosisSynthesizer）一次

`N` 通常等于 `normalized_candidates` 数量，接近 `top_k`（默认常见是 5）。
所以常见任务大约是 `8` 次 LLM 调用。

---

## 2. 分模块调用明细

| 阶段 | 模块 | 调用次数 | 当前是否触发 |
|---|---|---:|---|
| 初诊综合（仅病例） | `LlmInitialDiagnosisSynthesizer.synthesize` 第1次 | 1 | 是 |
| 初诊综合（证据融合） | `LlmInitialDiagnosisSynthesizer.synthesize` 第2次 | 1 | 是 |
| 逐病复核 | `LlmPerDiseaseVerifier.verify` | `N` | 是 |
| 最终综合 | `LlmFinalDiagnosisSynthesizer.synthesize` | 1 | 是 |
| 相似病例二次判别 | `DeepRareCaseSearcher` + `Check_Patient_Agent` | 每候选病例 1 次 | 否（当前 mode 不是这个） |
| arXiv 摘要 | `search_Arxiv` -> `Summarize_Agent` | 每篇 1 次 | 否（当前关闭） |
| Wikipedia 摘要 | `search_Wiki` -> `Summarize_Agent` | 每篇 1 次 | 否（当前关闭） |

---

## 3. 每次调用的 Prompt（模板）

下面按真实代码中的 `system prompt` + `user prompt` 给出模板（保留占位符）。

## 3.1 初诊综合第 1 次：仅基于病例

位置：`src/yk_ferta/services/clinical_mvp.py` -> `LlmInitialDiagnosisSynthesizer.synthesize`（`case_only`）

### System prompt

```text
你是面向中文医生用户的临床推理助手。仅基于当前病例信息生成简短的第一轮鉴别诊断，并严格区分临床诊断和未确认的分子病因。
```

### User prompt（模板）

```text
只返回 JSON，不要输出 Markdown 或额外解释。JSON 结构为 {"candidates":[{"name":"...","score":0.0,"rationale":"...","supporting_phenotypes":["..."]}]}。最多给出 {top_k} 个候选疾病/综合征。

输出语言要求：除疾病英文名、HPO、OMIM、PubMed 标题、基因名等必要英文术语外，rationale 和 supporting_phenotypes 必须以中文为主。

候选命名规则：
{molecular_policy}

病例信息：
{patient_narrative}
```

---

## 3.2 初诊综合第 2 次：融合多源证据

位置：`src/yk_ferta/services/clinical_mvp.py` -> `LlmInitialDiagnosisSynthesizer.synthesize`（`merged`）

### System prompt

```text
你是罕见病/不孕不育诊断辅助系统的初诊综合模块。请整合表型、相似病例和检索知识，输出排序后的第一轮鉴别诊断。不要把未确认的基因假设写成已经确认的分子诊断。
```

### User prompt（模板）

```text
只返回 JSON，不要输出 Markdown 或额外解释。JSON 结构为 {"candidates":[{"name":"...","score":0.0,"rationale":"...","supporting_phenotypes":["..."]}]}。最多给出 {top_k} 个候选疾病/综合征。

输出语言要求：除疾病英文名、HPO、OMIM、PubMed 标题、基因名等必要英文术语外，rationale 和 supporting_phenotypes 必须以中文为主。

候选命名规则：
{molecular_policy}

病例信息：
{patient_narrative}

已确认表型：
{phenotype_block}

表型工具候选提示：
{hint_block}

本地相似病例：
{similar_case_block}

重要约束：role=testing_finding_reference 的案例是私有历史检测案例，不是最终临床诊断。只能作为表型/基因检测经验参考，不能当作确诊病例证据。

外部知识证据摘要：
{evidence_block}

仅基于病例的第一轮判断：
{case_only_output}
```

---

## 3.3 逐病复核：每个候选病 1 次

位置：`src/yk_ferta/services/clinical_mvp.py` -> `LlmPerDiseaseVerifier.verify`

### System prompt

```text
你正在审核某个候选疾病是否被当前证据充分支持。请区分临床支持、分子确认、反对证据和缺失信息。
```

### User prompt（模板）

```text
只返回 JSON，不要输出 Markdown 或额外解释。JSON 结构为 {"is_supported": true, "confidence": 0.0, "reasoning": "...", "evidence_ids": ["..."], "supporting_evidence": ["..."], "contradicting_evidence": ["..."], "missing_evidence": ["..."]}.

输出语言要求：reasoning 必须以中文为主；疾病英文名、HPO、OMIM、基因名、文献标题可保留英文。

候选疾病：{candidate_name}
标准化 ID：{candidate_id} / {ontology}

病例信息：
{patient_narrative}

患者表型：
{phenotype_block}

分子证据规则：
{molecular_policy}

本地疾病知识：
{disease_block}

相似病例：
{similar_case_block}

重要约束：role=testing_finding_reference 表示没有最终临床诊断的私有历史检测案例。它可以支持表型/基因检测相关性，但不能计为确诊诊断匹配。

如果候选病名或亚型暗示某个特定基因突变，请判断当前患者本人的分子证据是否支持该主张。没有患者本人的变异证据时，即使临床综合征合理，基因特异性结论也必须标为未确认。

候选病级证据与外部证据：
{candidate_evidence_block}

启发式信号：表型重叠={overlap}，支持性公共相似病例={support_case_count}
```

---

## 3.4 最终综合：整合为结构化诊断卡

位置：`src/yk_ferta/services/clinical_mvp.py` -> `LlmFinalDiagnosisSynthesizer.synthesize`

### System prompt

```text
你正在为中文医生用户生成简洁的诊断辅助摘要。请基于 DeepRare 风格证据链输出可追溯结论，并避免把未确认分子假设表述为已确认病因。
```

### User prompt（模板）

```text
只返回 JSON，不要输出 Markdown 或额外解释。JSON 结构为 {"summary":"...","diagnosis_cards":[{"disease_name_zh":"中文疾病名","disease_name_en":"English disease name or empty string","clinical_diagnosis":"...","support_level":"高|中|低","confidence":0.0,"omim_id":"300000 或 NA","omim_url":"https://www.omim.org/entry/300000 或空字符串","orphanet_id":"ORPHA:xxx 或 NA","orphanet_url":"...","inheritance":"...","disease_genes":["..."],"molecular_mechanism":"...","pathogenesis":"...","specialties":["..."],"supporting_evidence":["..."],"contradicting_evidence":["..."],"missing_evidence":["..."],"recommended_tests":["..."],"references":[{"title":"...","source_type":"omim|pubmed|orphanet","url":"...","citation":"..."}],"cautions":["..."]}],"next_steps":["..."],"cautions":["..."]}.

输出语言要求：summary、diagnosis_cards、next_steps、cautions 必须以中文为主；疾病英文名、HPO、OMIM、PubMed 标题、基因名等必要英文术语可保留英文。

疾病名展示要求：disease_name_zh 必须尽量给出中文疾病名。若没有标准中文译名，请给出医生能理解的中文译名并在 disease_name_en 保留英文原名；不要只把英文疾病名放在 disease_name_zh 中。

疾病分子信息要求：disease_genes 和 molecular_mechanism 表示该疾病在知识库中的致病基因、染色体区域或分子/遗传机制，不代表当前患者已经检出相关异常。如果证据不足，填写 NA 或“待确认”。不要输出“分子亚型”概念，也不要把另一个候选疾病当作本病的分子机制。

病例信息：
{patient_narrative}

已确认表型：
{phenotype_block}

分子证据规则：
{molecular_policy}

最终输出规则：必须区分临床诊断/鉴别诊断、疾病知识中的致病基因/机制、以及患者本人的检测结果。如果没有患者本人的变异或核型结果，请使用“疑似”“建议检测”等措辞，不要写成“由 X 基因导致”。

第一轮候选：
{candidate_block}

疾病标准化结果：
{normalized_block}

逐病种复核：
{review_block}

证据摘要：
{evidence_block}
```

---

## 4. 当前未触发但代码存在的 LLM 调用

### 4.1 Case Searcher 二次病例判别（当前关闭）

位置：`tools/llm_agent.py` -> `Check_Patient_Agent`

当使用 `DeepRareCaseSearcher` 且 `llm_filter=true` 时，会对每个召回病例调用：

```text
System:
Assume you are a doctor experienced in rare disease diagnosis, please judge if the two patient cases are likely to be the same disease based on the patient information. Please only output 'Yes' or 'No'

User:
Patient 1 phenotype: {query}
Patient 2 phenotype: {retrieved_case}
```

### 4.2 arXiv / Wikipedia 摘要（当前关闭）

位置：`tools/llm_agent.py` -> `Summarize_Agent`

当 `knowledge_searcher.arxiv_results > 0` 或 `wiki_results > 0` 时，对每篇内容调用：

```text
Assume you are a doctor, please summarize these medical article into a paragraph, only keep key message, mainly focus on the phenotype and related disease.
```

---

## 5. 一句话结论

在你当前本地配置下（不含外部 RAG-HPO），`yk-FERTA` 主流程每个任务的 LLM 调用为 `N+3` 次，典型 `N=5` 时约 `8` 次；调用主要集中在：

- 初诊两段综合（2次）
- 逐病复核（N次）
- 最终综合（1次）

