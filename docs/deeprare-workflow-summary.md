# DeepRare 工作流梳理

本文档面向项目汇报，聚焦 `DeepRare` 从临床病例输入到最终诊断输出的实际工作流，并说明每一步的输入、输出、所用资源，以及资源是在线还是本地。

## 1. 总体流程

当输入为自由文本临床病例时，`DeepRare` 的主流程可概括为：

1. 表型提取
2. 基于表型的候选疾病工具检索
3. 在线知识检索
4. 本地相似病例检索
5. LLM 生成第一轮诊断
6. 候选疾病标准化与逐病种校验
7. LLM 反思后输出最终诊断

如果有基因变异文件（VCF），则在上述流程后额外叠加 Exomiser 分析。

---

## 2. 分步骤说明

### Step 1. Phenotype Extractor

**目标**

从自由文本临床病例中提取 phenotype / HPO。

**输入**

- 自由文本病例信息

**输出**

- phenotype 描述列表
- HPO 标准化结果

**使用资源**

- OpenAI 模型，用于从病历文本抽 phenotype
- `definition2id.json`，phenotype 名称到 HPO ID 的映射
- `embeds_pheno.pt`，HPO 概念 embedding
- `phenotype_mapping.json`，HPO ID 到标准 phenotype 名称的映射
- `BioLORD` 模型，用于 phenotype 到 HPO 的语义映射

**在线 / 本地**

- 在线：
  - OpenAI API
- 本地：
  - `definition2id.json`
  - `embeds_pheno.pt`
  - `phenotype_mapping.json`
  - `BioLORD` 模型

---

### Step 2. Phenotype Analyser

**目标**

基于 HPO / phenotype 得到第一批候选疾病线索。

**输入**

- HPO ID 列表
- phenotype 列表

**输出**

- `PubCaseFinder` 返回的候选疾病
- `Phenobrain` 返回的候选疾病
- 可选的 HPO 官网相关疾病信息

**使用资源**

- PubCaseFinder API
- Phenobrain API
- HPO 官网页面

**在线 / 本地**

- 在线：
  - PubCaseFinder
  - Phenobrain
  - HPO 官网
- 本地：
  - 无核心知识库依赖

**说明**

这一阶段输出的是“候选线索”，不是最终初步诊断。

---

### Step 3. Knowledge Searcher

**目标**

补充与当前病例相关的在线医学知识。

**输入**

- 已标准化的 phenotype 文本（主输入）
- 部分场景下的 disease name / phenotype name

**输出**

- 网页搜索结果摘要
- PubMed 文献结果摘要
- arXiv 结果摘要
- Wikipedia 结果摘要

**使用资源**

- Bing / Google / DuckDuckGo
- PubMed
- arXiv
- Wikipedia
- 网页正文抓取与摘要模块
- LLM 摘要能力

**在线 / 本地**

- 在线：
  - Bing / Google / DuckDuckGo
  - PubMed
  - arXiv
  - Wikipedia
  - 各网页正文
  - LLM API
- 本地：
  - 无核心知识库存储

**说明**

这里不是离线文献知识库，而是运行时按需联网检索并摘要。`DeepRare` 的实现主要把已标准化的 phenotype 文本直接作为检索词，调用搜索引擎、PubMed、arXiv 和 Wikipedia 等在线资源；对于检索到的网页或条目内容，再通过正文抓取和 LLM 摘要得到后续诊断可用的证据文本。

该模块没有显式的 LLM 检索规划，工程上更接近“在线补充证据”。

---

### Step 4. Case Searcher

**目标**

从本地离线病例库中检索与当前病例最相似的病例，作为后续推理证据。

**输入**

- 当前病例文本
- 当前病例 phenotype / HPO 表示

**输出**

- 一组相似病例
- 每个相似病例的病例描述
- 每个相似病例的诊断结果

**使用资源**

- 本地多来源病例库：
  - `RDS_embeddings.csv`
  - `xinhua_rag_0331.csv`
  - `mimic_rag.csv`
  - `rarebench_rag.csv`
  - `mygene_rag.csv`
  - `ddd_rag.csv`
- OpenAI `text-embedding-3-small`，用于 query embedding
- 本地预计算病例 embedding
- `MedCPT-Cross-Encoder`，用于精排
- LLM `Yes/No` 过滤，用于判断病例是否真正相似

**在线 / 本地**

- 在线：
  - OpenAI embedding API
  - LLM API
- 本地：
  - 多来源病例库 CSV
  - 预计算 embedding
  - `MedCPT-Cross-Encoder` 模型

**说明**

这一步不是联网搜索病例，而是查询本地离线构建的病例向量库。

---

### Step 5. First-Round Diagnosis Synthesis

**目标**

把多路证据合并，生成第一轮 top-k 诊断。

**输入**

- Step 2 的 phenotype 工具结果
- Step 3 的在线知识摘要
- Step 4 的相似病例结果
- 仅用病例信息做出的LLM 初步诊断结果

**输出**

- 第一轮 top-k 疾病诊断
- 每个候选病的诊断理由
- 文内引用和参考文献列表

**使用资源**

- 主诊断 LLM

**在线 / 本地**

- 在线：
  - LLM API
- 本地：
  - 无新增知识库，消费前面步骤的结果

**说明**

这一步实际上做了两次 LLM 调用：

1. 第一次只看当前病例本身，让 LLM 直接给出一版初步诊断。
2. 第二次再把 phenotype 工具结果、在线知识、相似病例结果以及第一次的初步诊断一起合并，交给 LLM 生成第一轮更完整的 top-k 诊断。

---

### Step 6. Disease Normalizer

**目标**

把第一轮诊断里的候选疾病名标准化到统一疾病体系，便于后续精确查知识。

**输入**

- 第一轮 top-k 候选疾病名

**输出**

- 规范化后的疾病名
- 对应的 Orphanet / OMIM 标识

**使用资源**

- `orpha_concept2id.json`
- `orpha2name.json`
- `orpha2omim.json`
- `embeds_concept.pt`
- `BioLORD` 模型，用于疾病名 embedding 相似匹配

**在线 / 本地**

- 在线：
  - 无
- 本地：
  - `orpha_concept2id.json`
  - `orpha2name.json`
  - `orpha2omim.json`
  - `embeds_concept.pt`
  - `BioLORD` 模型

**说明**

这一步不是直接字符串匹配，而是“疾病名 embedding + 本地映射表”的标准化。

---

### Step 7. Per-Disease Verification

**目标**

对第一轮诊断里提到的每个候选病，逐个去补证据、做复核，判断这个病到底能不能站得住。

**输入**

- 当前病例 phenotype
- 相似病例证据
- 标准化后的候选疾病

**输出**

- 每个候选病的“支持 / 不支持”判断
- 每个候选病对应的支持理由和反驳理由

**使用资源**

- Orphanet 本地疾病知识
- OMIM
- PubMed
- arXiv
- Wikipedia
- 网页抓取与摘要
- `Check_Agent` LLM 校验模块

**在线 / 本地**

- 在线：
  - OMIM
  - PubMed
  - arXiv
  - Wikipedia
  - LLM API
- 本地：
  - `orpha_disorders_HP_map.json`
  - `orpha2omim.json`

**说明**

这一步不是再重新做一次泛化诊断，而是对前一步已经给出的候选病逐个做“审核”。

可以把它理解成：

1. 前一步先提出“可能是什么病”。
2. 这一步再逐个检查“这个候选病有没有足够证据成立”。

做法是先把候选病名标准化到可检索的疾病实体，再补充该病在 Orphanet、OMIM、PubMed、Wikipedia 等来源里的相关信息，最后交给 `Check_Agent` 判断该病是否被当前病例支持。

因此，这一步输出的不是新的 top-k 诊断，而是每个候选病的复核意见，供最后一步重新整合。

---

### Step 8. Final Diagnosis Synthesis

**目标**

结合第一轮诊断和逐病种校验结果，生成最终诊断输出。

**输入**

- 当前病例信息
- 相似病例结果
- 第一轮诊断结果
- 每个候选病的校验结果

**输出**

- 最终 top-k 诊断
- 最终诊断理由
- 引用链和参考文献

**使用资源**

- 主诊断 LLM

**在线 / 本地**

- 在线：
  - LLM API
- 本地：
  - 无新增知识库，消费前面步骤的结果

---

## 3. 如果有基因数据的附加步骤

### Step 9. Genotype Analyser（可选）

**目标**

结合 VCF 与 phenotype，对最终诊断进行基因层面的增强。

**输入**

- VCF 文件
- HPO 列表
- phenotype 版初步诊断结果

**输出**

- Exomiser 候选基因 / 变异结果
- 基因增强后的最终诊断

**使用资源**

- Exomiser
- ClinVar / gnomAD / 1000G / UK10K / TOPMed 等 Exomiser 使用的数据源
- LLM 融合 Exomiser 结果

**在线 / 本地**

- 在线：
  - LLM API
- 本地：
  - Exomiser
  - 本地 VCF
  - Exomiser 相关数据库

---

## 4. 资源归类总结

### 本地资源

- Orphanet / OMIM 映射文件
- phenotype / disease 标准化字典
- HPO / disease embedding 索引
- 多来源离线病例库
- `BioLORD` 模型
- `MedCPT-Cross-Encoder`
- Exomiser 及其本地数据

### 在线资源

- OpenAI / Gemini / DeepSeek / Claude 等 LLM API
- PubCaseFinder
- Phenobrain
- HPO 官网
- Bing / Google / DuckDuckGo
- PubMed
- arXiv
- Wikipedia
- OMIM 页面内容

---

## 5. 一句话总结

`DeepRare` 的核心不是单一知识库问答，而是：

**先把病例转成 phenotype/HPO，再把 phenotype 工具、本地病例库、结构化疾病映射和在线知识检索一起作为证据源，由 LLM 先生成候选诊断，再逐病种校验，最后输出可追溯的最终诊断。**
