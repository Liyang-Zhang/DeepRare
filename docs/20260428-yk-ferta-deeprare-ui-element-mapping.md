# 20260428 yk-FERTA DeepRare UI Element Mapping

## 目的

本文档用于确认 DeepRare 网页版当前展示的主要元素，在 yk-FERTA 本地工作流中是否已有可用数据支撑，并明确：

1. 哪些区块可以直接复刻。
2. 哪些区块需要展示层转换。
3. 哪些区块当前仍缺结构化产物，需要后续补充。

本文档面向后续前端界面改造，重点是 `task_viewer` 和正式演示页的信息映射，不涉及视觉设计本身。

## 适用范围

当前分析基于本地 task-managed workflow 的输出：

- `result`
- `artifacts/hpo`
- `artifacts/phenotype_hints`
- `artifacts/phenotype_tools`
- `artifacts/evidence`
- `artifacts/similar_cases`
- `artifacts/preliminary_diagnosis`
- `artifacts/normalized_candidates`
- `artifacts/reviews`
- `artifacts/final_report`

如果后续某些页面直接调用 `ClinicalMvpPipeline.run(...)` 的同步路径，需要确认其输出字段与 task workflow 对齐。

## 总体判断

当前本地工作流已经可以支撑 DeepRare 展示页中大部分核心区块。

整体判断如下：

- 可直接支撑：`病例库检索`、`反思判断`、`最终诊断建议卡`
- 可较低成本复刻：`医学文献检索结果`、`五种可能诊断及概览`
- 当前主要缺口：`诊断标准检索`

关键结论：

- 问题不在于底层完全没有数据。
- 问题主要在于缺少一层稳定的展示映射模型。
- 最明显的结构化缺口是“诊断标准/主要标准/次要标准”这一类专门产物。

## DeepRare 展示区块映射

### 1. 医学文献检索结果

DeepRare 展示元素：

- 资料来源
- 主要发现
- 相关性
- 临床价值

本地对应来源：

- `result.response.knowledge_evidence`
- `artifacts/evidence`
- `artifacts/candidate_evidence`（若存在）

本地现有字段：

- `source_type`
- `title`
- `summary`
- `citation`
- `url`
- `relevance_score`

映射判断：

- `资料来源`：可直接由 `source_type + citation/title` 生成。
- `主要发现`：可直接使用 `summary`。
- `相关性`：可基于 `relevance_score` 分桶显示，例如高/中/低相关。
- `临床价值`：当前无显式字段，需要展示层基于 `summary + source_type` 做轻量归纳，或后续后端新增字段。

实现建议：

- 第一版可直接展示：`来源 / 标题 / 摘要 / 相关性`。
- `临床价值` 可先作为展示层衍生字段，不必立即新增后端 schema。

结论：

- 可复刻基础版。
- `临床价值` 不是现成字段，需要额外整理。

### 2. 诊断标准检索

DeepRare 展示元素：

- 主要标准
- 次要标准
- 文献参考

本地可用来源：

- `artifacts/normalized_candidates`
- `artifacts/reviews`
- `artifacts/evidence`
- disease-specific Orphanet / OMIM knowledge

当前现状：

- 本地已有 disease knowledge 和 review reasoning。
- 但没有显式结构化的：
  - `major_criteria`
  - `minor_criteria`
  - `criteria_reference`

映射判断：

- `文献参考`：可以从 `candidate_evidence` 或 `references` 中提取。
- `主要标准 / 次要标准`：当前无法稳定、结构化地直接提供。

风险：

- 如果直接从 `review.reasoning` 或 `summary` 中切句子，容易得到不稳定展示。
- 这类内容更适合变成独立 artifact 或独立展示模型。

实现建议：

- 第一版不要强行完全复刻。
- 可先显示“与候选疾病相关的诊断依据/知识摘要”。
- 后续可新增专门产物：
  - `candidate_criteria`
  - 每个候选病包含：
    - `major_criteria`
    - `minor_criteria`
    - `criteria_reference`

结论：

- 这是当前最主要的结构化缺口。
- 暂不建议在前端直接伪造同形态展示。

### 3. 病例库检索

DeepRare 展示元素：

- 相似病例列表
- 病例摘要
- 与当前病例的关系说明

本地对应来源：

- `result.response.similar_cases`
- `artifacts/similar_cases`

本地现有字段：

- `case_id`
- `source`
- `summary`
- `diagnosis`
- `score`
- `evidence_role`
- `disease_id`
- `reported_genes`
- `phenotype_relevant_genes`
- `variant_summary`
- `metadata`

映射判断：

- 可直接做相似病例卡片。
- `evidence_role` 可明确区分：
  - `diagnosis_reference`
  - `testing_finding_reference`
- 这点甚至比 DeepRare 原版更符合产品审计要求。

展示建议：

- 公共病例和私有检测参考分组展示。
- 私有历史检测案例必须显式标注“检测发现参考”，不能写成确诊相似病例。

结论：

- 这一块可直接支撑。
- 前端只需要做良好的信息组织。

### 4. 五种可能诊断及概览

DeepRare 展示元素：

- 可能诊断
- 估计概率
- 支持证据
- 反对证据

本地对应来源：

- `artifacts/preliminary_diagnosis`
- `artifacts/reviews`
- `result.response.final_recommendation.diagnosis_cards`

本地现有字段：

- 初诊候选：`name`、`score`、`rationale`
- 复核结果：`is_supported`、`confidence`、`supporting_evidence`、`contradicting_evidence`、`missing_evidence`
- 最终卡片：`support_level`、`confidence`

映射判断：

- `可能诊断`：可直接取初诊候选名或标准化后的名称。
- `支持证据`：已有。
- `反对证据`：已有。
- `估计概率`：当前没有严格意义上的概率。

展示建议：

- 第一版建议用“置信度”或“支持度”，不要直接称为“概率”。
- 如果要保留百分比视觉样式，需要明确它是相对支持度，不应被表述为严格统计概率。

结论：

- 可复刻。
- 命名上应谨慎，避免误导。

### 5. 反思判断

DeepRare 展示元素：

- 每个候选的复核卡
- 支持证据
- 反对证据
- 批判反思

本地对应来源：

- `artifacts/reviews`

本地现有字段：

- `candidate_name`
- `is_supported`
- `confidence`
- `reasoning`
- `supporting_evidence`
- `contradicting_evidence`
- `missing_evidence`

映射判断：

- `支持证据`：直接对应。
- `反对证据`：直接对应。
- `批判反思`：可直接使用 `reasoning`。
- `缺失证据`：本地还有额外的 `missing_evidence`，可作为增强项。

结论：

- 这一块已经具备完整支撑能力。
- 几乎可以按 DeepRare 版式直接复刻。

### 6. 最终诊断建议卡

DeepRare 展示元素：

- 疾病名称
- 概率/支持度
- OMIM
- 遗传方式
- 致病基因
- 推荐检查
- 发病机制
- 推荐理由
- 多学科会诊推荐
- 相关知识库和参考资源

本地对应来源：

- `result.response.final_recommendation.diagnosis_cards`
- `artifacts/final_report`
- `artifacts/result`

本地现有字段：

- `disease_name_zh`
- `disease_name_en`
- `clinical_diagnosis`
- `support_level`
- `confidence`
- `omim_id`
- `omim_url`
- `orphanet_id`
- `orphanet_url`
- `inheritance`
- `disease_genes`
- `molecular_mechanism`
- `pathogenesis`
- `specialties`
- `supporting_evidence`
- `contradicting_evidence`
- `missing_evidence`
- `recommended_tests`
- `references`
- `cautions`

映射判断：

- `疾病名称`：可直接使用，中文优先。
- `支持度`：已有 `support_level/confidence`。
- `OMIM / Orphanet`：已有。
- `遗传方式`：已有。
- `致病基因`：已有，但必须保持疾病层知识语义，不可误写为患者已确认。
- `推荐检查`：已有。
- `发病机制`：已有。
- `推荐理由`：没有单独字段，但可由 `supporting_evidence + review.reasoning` 聚合生成。
- `多学科会诊推荐`：当前只有 `specialties`，足以支撑第一版展示。
- `相关知识库和参考资源`：已有 `references`。

结论：

- 这一块已基本具备完整支撑能力。
- 需要的主要是展示层归纳，而不是后端新增大量字段。

## 可复刻性总结

### 可直接复刻

- 病例库检索
- 反思判断
- 最终诊断建议卡

### 可低成本复刻

- 医学文献检索结果
- 五种可能诊断及概览

### 当前缺口明显

- 诊断标准检索

## 当前最主要缺口

### 1. 诊断标准的结构化表达

当前缺少稳定的：

- `major_criteria`
- `minor_criteria`
- `criteria_reference`

这是与 DeepRare 截图相比最主要的结构化差异。

### 2. 文献“临床价值”字段

当前有文献摘要，但没有明确的结构化 `clinical_value`。

### 3. 推荐理由独立字段

当前可从多个字段拼出，但不是单独后端字段。

### 4. 多学科会诊建议对象

当前只有 `specialties`，没有更强的会诊建议结构。

## 前端改造建议

不建议前端直接硬拼所有 artifact 原始 JSON。

推荐顺序：

1. 先定义展示映射层。
2. 再基于该映射层改造 `task_viewer`。
3. 最后再考虑是否为“诊断标准检索”新增专门产物。

建议优先做的展示视图模型：

- `literature_panel_view`
- `similar_cases_panel_view`
- `candidate_overview_view`
- `review_cards_view`
- `final_diagnosis_card_view`

这些视图模型可以先在前端本地转换，也可以后续逐步上收至后端。

## 建议的下一步

1. 先以本文件为依据，梳理 `task_viewer` 的展示区块改造清单。
2. 对“诊断标准检索”单独立项，不要在本轮前端复刻中强行补齐。
3. 对最终诊断卡优先使用中文疾病名称和中文说明。
4. 所有私有病例引用必须保留 `testing_finding_reference` 语义，不可展示成确诊支持证据。

## 一句话结论

DeepRare 当前网页展示的大部分核心元素，在 yk-FERTA 本地工作流中已经有数据基础；真正需要补的不是“更多后端结果”，而是“稳定的展示映射层”，以及少量尚未结构化的诊断标准类产物。
