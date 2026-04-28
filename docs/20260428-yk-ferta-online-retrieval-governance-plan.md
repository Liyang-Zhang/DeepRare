# yk-FERTA 在线资料搜集模块治理待办（20260428）

本文档记录 yk-FERTA 当前“初诊前在线资料搜集”模块的现状、与 DeepRare 原始实现的对比，以及后续治理待办。

目标不是增加更多来源，而是让进入初诊阶段的在线资料：

1. 来源边界更清晰
2. 可信度更稳定
3. 噪声更可控
4. 在 traceable workflow 中可审计

## 1. 当前范围

当前初诊前会进入工作流的在线资料主要包括：

1. phenotype-level 工具
- `phenobrain`
- `hpo_association`
- `pubcasefinder`（代码支持，默认关闭）

2. patient / phenotype oriented knowledge search
- 通用 Web 搜索
- PubMed
- ArXiv（默认关闭）
- Wikipedia（默认关闭）

3. 非在线但同层进入初诊的资料
- 公共病例库相似病例
- 私有历史检测案例库相似病例

本文档只讨论在线来源治理，不讨论本地病例库权重治理。

## 2. 当前实现现状

### 2.1 phenotype tools

当前默认配置：

- `enable_pubcasefinder = false`
- `enable_phenobrain = true`
- `enable_hpo_association = true`
- `hpo_association_top_n = 5`

现实表现：

1. `phenobrain`
- 代码已接入
- 当前上游服务存在 `500`，可靠性不足
- 现阶段只能作为 best-effort hint

2. `hpo_association`
- 已从 Selenium 网页抓取替换为 HPO 官方 API
- 目前按 `Top N` 截断，默认 `Top 5`
- 仍存在明显噪声 disease hint

3. `pubcasefinder`
- 代码位点保留
- 当前默认关闭
- 尚未重新评估稳定性和实际增益

### 2.2 knowledge searcher

当前默认配置：

- `search_engine = duckduckgo`
- `web_results = 3`
- `pubmed_results = 3`
- `arxiv_results = 0`
- `wiki_results = 0`

当前真实主干：

1. `Web search`
- 默认走 `ddgs`
- 可 fallback 到 `duckduckgo / brave / google / bing`

2. `PubMed`
- 直接走 NCBI E-utilities
- 已有简单相关性打分 `_score_pubmed_article(...)`

3. `ArXiv / Wikipedia`
- 代码支持
- 默认不进入流程

## 3. DeepRare 原始实现对比

### 3.1 DeepRare 原始资料搜集

DeepRare 原始源码在初诊前主要使用：

1. 对 phenotype 的：
- Web search
- HPO 官网相关疾病
- PubMed

2. diagnosis API results：
- `PubCaseFinder`
- `Phenobrain`

3. 对 patient narrative 的：
- Web diagnosis

### 3.2 DeepRare 原始过滤方式

DeepRare 原始源码没有形成明确的“高可信在线证据优先级系统”。

它的典型策略是：

1. 宽召回
2. 很少做前置可信度过滤
3. 依赖后续 `Check_Agent` / 反思阶段做语义判断

具体看：

1. Web search
- 没有域名白名单
- 没有医学站点优先级
- 没有发布时间过滤
- 没有低可信网页剔除策略
- 只有“页面能不能读”的技术性过滤

2. PubMed
- 没有按文章类型分层
- 没有按发表年份过滤
- 没有按期刊/证据等级过滤
- 基本是 `top_k + LLM 摘要`

3. HPO search
- 基本是 `top5 related diseases`
- 没有领域过滤或重排

结论：

DeepRare 值得借鉴的是“宽召回后反思”的总体思路，而不是它的在线资料过滤策略。

## 4. 当前问题

### 4.1 在线来源可信度没有显式分层

目前进入初诊前信息层的在线来源，虽然在 `source_type` 上可区分，但还没有真正形成权重语义。

例如：

- `PubMed`
- `Orphanet`
- 普通 Web 页面

在当前 prompt 和下游消费里，边界还不够硬。

### 4.2 Web search 噪声仍偏大

当前 Web 搜索仍然属于通用召回：

- 可能混入低质量网页
- 可能混入非临床决策价值网页
- 没有来源优先级

### 4.3 PubMed 仍缺少医学证据分层

当前 PubMed 已经比 DeepRare 原始实现更稳，但仍没有：

- guideline / review / cohort / case report 分类
- 时间衰减
- fertility / reproductive genetics 领域优先级

### 4.4 phenotype tool 的稳定性和噪声仍影响初诊

1. `phenobrain` 当前不稳定
2. `hpo_association` 虽已可用，但噪声较高
3. `pubcasefinder` 还未重新评估

## 5. 产品原则

后续治理必须符合 yk-FERTA 的产品哲学：

1. 在线资料搜集是“候选支持层”，不是“裁决层”
2. 不要用 brittle disease-specific hard code 解决某一个 demo case
3. 允许外部工具失败，但失败不能拖垮主流程
4. 证据来源和可信度层级必须可追溯
5. 初诊阶段优先提高“可用信息质量”，而不是单纯堆来源数量

## 6. 治理路线

### 6.1 P0：来源语义收口

目标：先把不同来源在工作流中的角色说清楚。

待办：

1. 明确来源层级
- `orphanet / omim / pubmed / web_search / phenotype_tool_hint`

2. 在 prompt 和下游逻辑中明确：
- `phenotype_tool_hint` 只是候选提示
- `web_search` 只是补充背景
- `pubmed` 是文献级证据，但仍需分层

3. 在 artifact 中保留来源、query、rank、错误、耗时

### 6.2 P1：PubMed 质量分层

目标：让 PubMed 从“可用”提升到“更像医学证据”。

待办：

1. 引入文章类型分类
- guideline
- review
- cohort / original study
- case report
- other

2. 引入时间因子
- 近 5-10 年优先
- 经典高相关老文保留但降权

3. 对 fertility / reproductive genetics 关键词做加权

4. 明确 candidate-level PubMed 与 global PubMed 的角色差异

### 6.3 P1：Web 来源优先级

目标：减少泛网页噪声。

待办：

1. 对域名做优先级分层
- 高优先：`orpha.net`, `omim.org`, `ncbi.nlm.nih.gov`, `medlineplus.gov`
- 中优先：专业医疗机构 / 学会 / 大学医院
- 低优先：普通泛科普网页

2. 允许 Web 继续宽召回，但进入 evidence 前先重排

3. 普通网页不应与 PubMed / Orphanet 同权进入初诊主证据块

### 6.4 P1：phenotype tools 重新评估

目标：把 phenotype-driven 召回层做稳。

待办：

1. `phenobrain`
- 视为外部不稳定依赖
- 保持 best-effort
- 后续只在服务质量恢复后再评估其价值

2. `hpo_association`
- 继续保留 `Top N`
- 后续可考虑轻量重排，但不做复杂病种特判

3. `pubcasefinder`
- 重新验证可用性
- 如果稳定，优先级高于 ArXiv / Wikipedia

### 6.5 P2：统一 evidence ranking 语义

目标：让“进入初诊前”的在线资料都有统一排序逻辑。

建议引入综合分：

- source prior
- query relevance
- recency
- article type / page type
- domain relevance

但这一层在实现上应保持可解释，不要做黑箱大一统分数。

## 7. 当前不建议优先做的事

1. 打开 `ArXiv`
- 对当前不孕不育临床场景边际价值低

2. 打开 `Wikipedia`
- 可作为辅助背景，不适合作为优先主证据来源

3. 为某个测试病例单独加 query hack 或来源 hard code
- 会污染整体性能判断

## 8. 推荐执行顺序

1. `P0` 明确来源语义和下游消费边界
2. `P1` 做 PubMed 分层
3. `P1` 做 Web 来源优先级
4. `P1` 重新评估 `PubCaseFinder`
5. `P2` 最后再统一 evidence ranking

## 9. 验收标准

这块治理完成后，应至少满足：

1. 初诊前在线资料来源层级清晰
2. 普通网页不会和高可信医学来源同权表达
3. PubMed 结果具备基础类型/时间分层
4. 外部 phenotype tool 失败不会拖垮主流程
5. 所有在线资料来源都能在 artifact 中回溯

