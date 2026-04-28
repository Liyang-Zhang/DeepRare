# 不孕不育公共病例库表结构

本文档定义 `yk-FERTA` 公共病例库的第一版表结构。设计目标是兼容 `DeepRare` 本地病例库格式，同时增加不孕不育专病筛选、溯源和后续清洗需要的字段。

## 1. 当前输出文件

- `database/fertility_public_cases_rds.csv`
- `database/fertility_public_cases_rds.stats.json`
- 生成脚本：`scripts/build_fertility_public_case_bank.py`

## 2. 字段设计

### DeepRare 兼容字段

这些字段用于兼容现有 Case Searcher：

- `_id`：病例唯一 ID。当前格式为 `rds:<source_record_id>`。
- `case_report`：病例文本摘要，后续用于 embedding 和相似病例检索。
- `diagnosis`：原始诊断名称。
- `Orpha_name`：RareArena / RDS 提供的 Orphanet 疾病名。
- `Orpha_id`：Orphanet ID。
- `age`：年龄原始字段，JSON 字符串保存。
- `gender`：性别。
- `embedding`：病例向量。当前为空，后续构建 embedding 后填充。

### yk-FERTA 扩展字段

这些字段用于专病病例库管理：

- `source_dataset`：来源数据集。当前为 `RareArena_RDS`。
- `source_record_id`：原始记录 ID。
- `source_pub_date`：原始病例发表日期。
- `source_pmid`：基于 `PMC-Patients-V2.json.patient_uid == source_record_id` 精确回填的 PubMed ID。
- `source_title`：来自 `PMC-Patients-V2.json` 的文章标题。
- `source_file_path`：来自 `PMC-Patients-V2.json` 的原始文件路径。
- `source_url`：基于回填的 PMID 生成的 PubMed URL；未匹配时为空。
- `matched_terms`：命中的不孕不育相关关键词，使用 `|` 分隔。
- `matched_categories`：命中关键词所属类别，使用 `|` 分隔。
- `fertility_relevance_score`：基于关键词权重的相关性分数。
- `fertility_relevance_tier`：相关性层级，取值为 `strong / moderate / weak`。
- `case_text_length`：病例文本长度，便于后续过滤过短或异常文本。

## 3. 当前筛选策略

输入文件：

- `database/RDS.json`

筛选方式：

- 按 JSONL 逐行读取。
- 在 `case_report + diagnosis + Orpha_name` 上做宽松关键词匹配。
- 关键词覆盖不孕不育、复发性流产、葡萄胎、卵巢功能、男性因素、ART/配子胚胎、部分生殖解剖疾病。
- 当前策略偏召回，允许纳入弱相关和少量非不孕不育病例，后续再做二次清洗。

## 4. 当前筛选结果

从 `RDS.json` 中：

- 总记录数：`49,760`
- 纳入记录数：`1,596`
- 强相关：`344`
- 中等相关：`717`
- 弱相关：`535`

2026-04-28 更新：

- 公共病例库构建脚本已接入 `database/PMC-Patients-V2.json`。
- 基于 `source_record_id == patient_uid` 做精确回填，可为当前 `1,596` 条筛入记录全部补齐：
  - `source_pmid`
  - `source_title`
  - `source_file_path`
  - `source_url`
- 这意味着后续公共病例检索结果可稳定展示 `PMID` 和 PubMed 链接，无需模糊匹配。

主要类别：

- `ovarian_function`
- `male_factor`
- `reproductive_anatomy`
- `infertility`
- `pregnancy_loss`
- `art_gamete_embryo`
- `molar_pregnancy`
- `oocyte_maturation`
- `dsd`

2026-04-22 更新：

- 根据医学部补充词表追加了 `premature ovarian insufficiency`、`oocyte maturation arrest`、`hypoplasia of the fallopian tube`、`abnormal sperm morphology`、`oligozoospermia`、`azoospermia`、`disorders of sex development` 等方向的关键词和同义词。
- 相比上一版新增约 `79` 条净记录，主要来自 DSD / hypospadias / ambiguous genitalia / androgen insensitivity 等方向。
- `DSD` 缩写已改为词边界匹配，避免误匹配 `anti-dsDNA` 等无关文本。

## 5. 后续处理建议

下一步不应直接把全部 1,517 条作为高置信病例库使用，而应分层使用：

- `strong`：优先进入 MVP 相似病例库。
- `moderate`：可进入候选库，但检索时降低权重。
- `weak`：暂时保留用于召回，不直接参与最终诊断证据，或仅在无 strong/moderate 命中时补充。

后续需要补充：

- embedding 生成。
- 与 PMC-Patients 公共病例库合并。
- 去重。
- 诊断名标准化。
- 不孕不育专病标签体系。
