# yk-FERTA 临床信息版 MVP 设计

本文档给出一版按 `DeepRare` 工作流重构后的 `yk-FERTA` 最小可用产品设计。目标不是立即做成完整临床系统，而是先用本地可控、可替换、可测试的工程骨架，复刻 `DeepRare` 的核心逻辑。

当前范围只覆盖：

- 输入：临床信息
- 输出：候选病因/诊疗方向 + 证据 + 复核结果 + 最终可追溯输出
- 不包含：VCF、变异注释、Exomiser、基因优先级分析

## 1. MVP 目标

这一版 MVP 的目标很明确：

1. 复刻 `DeepRare` 的主工作流，而不是复刻它的研究代码结构。
2. 所有阶段都拆成独立组件，便于替换真实能力。
3. 先打通“临床信息 -> 多源证据 -> 第一轮候选 -> 逐候选复核 -> 最终输出”。
4. 所有外部能力都通过 adapter 接口挂接，不把 prompt、检索和业务逻辑写死在单个脚本里。

## 2. MVP 工作流

临床信息版 MVP 保持和 `DeepRare` 一致的阶段顺序：

1. `Phenotype Extractor`
2. `Phenotype Analyser`
3. `Knowledge Searcher`
4. `Case Searcher`
5. `First-Round Diagnosis Synthesis`
6. `Disease Normalizer`
7. `Per-Disease Verification`
8. `Final Diagnosis Synthesis`

对应的逻辑是：

1. 从临床文本里抽取 phenotype。
2. 用 phenotype 工具得到第一批候选线索。
3. 检索知识证据。
4. 检索本地相似病例。
5. 合并多路证据生成第一轮候选。
6. 把候选疾病名标准化到统一实体。
7. 对每个候选逐个补证据并复核。
8. 基于第一轮候选和复核结果生成最终输出。

## 3. 与 DeepRare 的对应关系

这版 MVP 保留 `DeepRare` 的方法链，但做了工程上的重构：

- 保留：
  - 多源证据输入
  - 两阶段诊断生成
  - 候选病标准化
  - 逐病种复核
  - 可追溯输出

- 不保留：
  - 单脚本串行编排
  - 大 prompt 包办所有逻辑
  - 数据文件和业务逻辑强耦合
  - 在线资源和核心逻辑强绑定

## 4. 代码结构

当前已经落下来的 MVP 结构是：

- `src/yk_ferta/schemas/mvp.py`
  - 定义各阶段中间结果
- `src/yk_ferta/adapters/clinical_mvp.py`
  - 定义临床信息版 MVP 的阶段接口
- `src/yk_ferta/pipelines/clinical_mvp.py`
  - 定义主编排 pipeline
- `src/yk_ferta/services/clinical_mvp.py`
  - 提供可运行的默认占位实现

## 5. 阶段接口设计

### 5.1 Phenotype Extractor

输入：

- `PatientProfile`

输出：

- `list[PhenotypeItem]`

职责：

- 从临床文本中提取标准化 phenotype
- 当前阶段可以先接你们已有的“病例 -> 表型”智能体

### 5.2 Phenotype Analyser

输入：

- `PatientProfile`
- `list[PhenotypeItem]`

输出：

- `list[PhenotypeToolHit]`

职责：

- 模拟 `DeepRare` 的 `PubCaseFinder / Phenobrain / HPO-like` 候选线索阶段
- 在不孕不育场景下，后续可以替换为领域化 phenotype 工具或规则引擎

### 5.3 Knowledge Searcher

输入：

- `PatientProfile`
- `list[PhenotypeItem]`

输出：

- `list[EvidenceItem]`

职责：

- 检索知识库、指南、内部知识卡、结构化数据库
- MVP 阶段建议优先本地知识，在线资源只做补充

### 5.4 Case Searcher

输入：

- `PatientProfile`
- `list[PhenotypeItem]`

输出：

- `list[SimilarCase]`

职责：

- 查询本地病例库
- 模拟 `DeepRare` 的“病例向量检索 + 重排 + 过滤”模式

### 5.5 First-Round Diagnosis Synthesis

输入：

- phenotypes
- phenotype hints
- knowledge evidence
- similar cases

输出：

- `list[CandidateCondition]`

职责：

- 对应 `DeepRare` 的第一轮候选生成
- 工程上建议拆成两段：
  - 第一段：仅基于病例初步生成
  - 第二段：证据增强后生成第一轮候选

### 5.6 Disease Normalizer

输入：

- `list[CandidateCondition]`

输出：

- `list[NormalizedDisease]`

职责：

- 把候选名称对齐到统一疾病实体
- 后续替换成 fertility 领域术语标准化和映射能力

### 5.7 Per-Disease Verification

输入：

- patient
- phenotypes
- similar cases
- knowledge evidence
- normalized candidates

输出：

- `list[CandidateReview]`

职责：

- 对每个候选病逐个做审核
- 判断每个候选是否真正被当前病例支持
- 这是 MVP 里最关键的“DeepRare 风格”模块之一

### 5.8 Final Diagnosis Synthesis

输入：

- 前面所有阶段结果

输出：

- `TraceableRecommendation`

职责：

- 汇总第一轮候选和复核结果
- 输出最终可追溯建议

## 6. MVP 推荐实现策略

为了先把链路跑通，建议按下面的顺序接入真实能力：

1. `Phenotype Extractor`
   - 先接你们已有本地智能体
2. `Knowledge Searcher`
   - 先接本地知识库
3. `Case Searcher`
   - 先接本地病例库
4. `First-Round Diagnosis Synthesis`
   - 先做一个受控 prompt 版本
5. `Disease Normalizer`
   - 先用本地词典/映射
6. `Per-Disease Verification`
   - 先做规则 + LLM 的混合版
7. `Final Diagnosis Synthesis`
   - 再做最终输出整合

## 7. 本地服务形态

这版 MVP 最适合做成本地单服务，而不是多服务部署：

- 一个本地 API 服务
- 内部挂接：
  - phenotype extractor
  - knowledge retriever
  - case retriever
  - diagnosis synthesizer
  - normalizer
  - verifier

这样做的原因是：

- 先保证链路可控
- 先把数据流和接口稳定下来
- 后续再按性能和职责拆服务

## 8. 当前代码状态

当前仓库里已经有一版可运行骨架：

- `build_clinical_mvp_pipeline()`
  - 返回 DeepRare 风格的临床信息 MVP pipeline
- 所有阶段默认是 placeholder 实现
- 单元测试已覆盖基本链路

这意味着后续开发时，不需要再重新设计阶段，只需要逐个替换 adapter 的真实实现。

## 9. 一句话总结

这版 `yk-FERTA` 临床信息 MVP 的核心原则是：

**保留 `DeepRare` 的“多源证据 -> 第一轮候选 -> 候选标准化 -> 逐项复核 -> 最终输出”逻辑，把它重构成一条本地可控、模块化、可替换、可测试的工程 pipeline。**
