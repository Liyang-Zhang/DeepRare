# DeepRare 网页端工作流观察与 yk-FERTA 后续开发规划

本文档基于 `deeprare-web-f12-test.txt` 中复制的 DeepRare 网页端 SSE 响应，以及当前 yk-FERTA 本地 MVP 的实现状态，整理网页端实际产品工作流、关键发现、与本地实现的差距，以及后续开发优先级。

## 1. 网页端实际工作流

DeepRare 网页端不是一次性返回最终诊断，而是通过 SSE 持续推送阶段性事件。一次完整任务大致包含以下阶段：

1. `started`
   - 加载病例和已确认 HPO。
   - 本例输入为“不孕不育 + 两次葡萄胎妊娠”，临床表型为 2 个。

2. `setup`
   - 生成病例诊断 prompt。
   - 这是后续 LLM 诊断的基础输入。

3. `api_diagnosis`
   - 调用表型诊断 API。
   - 本例实际返回的是 Phenobrain 结果：
     - `HYDM2`
     - `HYDM3`
     - `HYDM1`
     - `SPGF3`
     - `SPGF27`
   - 虽然提示文案写了 PubCaseFinder 和 Phenobrain，但本次响应中只看到 Phenobrain 明确结果。

4. `parallel_diagnosis`
   - 并行执行三类任务：
     - 网络搜索
     - LLM 基于病例的初步诊断
     - 相似病例检索
   - 本例第一轮返回了与复发性葡萄胎、NLRP7/KHDC3L、辅助生殖后葡萄胎相关的网页/文献搜索结果。

5. `comprehensive_analysis`
   - 综合表型 API、网络搜索、LLM 初诊、相似病例，生成第一版 top 诊断。
   - 本例第一轮综合诊断将 `Familial Recurrent Hydatidiform Mole` 放在首位。

6. `disease_matching`
   - 从综合诊断文本中抽取疾病名。
   - 与 Orphanet 等标准疾病体系做语义匹配。
   - 对匹配到的疾病逐个做证据复核。
   - 如果匹配度较低，会进入下一轮更深搜索。

7. `search_depth_increase`
   - 当候选病匹配或复核结果不理想时，提高搜索深度。
   - 本例至少经历了多轮搜索深度提升。

8. `final_comprehensive_diagnosis`
   - 生成最终综合诊断文本。

9. `result_*` 结构化结果阶段
   - `result_hpo`
   - `result_inforetrieval`
   - `result_preliminarydiagnosis`
   - `result_reflexion`
   - `result_suggest`
   - `diagnosisresult`

10. `completed`
   - 聚合所有 artifact，并返回最终结果包。

## 2. 网页端输出结构的关键发现

DeepRare 网页端的最终结果不是单一文本，而是一个多 artifact 结果包。

核心结构包括：

- `result_hpo`
  - 临床症状与体征
  - 提取的 HPO
  - 表型组合分析
  - HPO 与候选疾病匹配分析
  - HPO 分析结论

- `result_inforetrieval`
  - 医学文献发现
  - 诊断标准
  - 相似病例
  - 资料检索结论

- `result_preliminarydiagnosis`
  - 五种可能诊断
  - 每个诊断的概率
  - 支持证据
  - 反对证据
  - 初步诊断结论

- `result_reflexion`
  - 对每个候选病做批判性反思
  - 区分“能解释所有表现的病因诊断”和“只能解释局部表现的干扰项”

- `result_suggest`
  - 最终诊断
  - 诊断信心
  - 亚型
  - 诊断依据
  - 治疗/生育方案
  - 随访建议

- `diagnosisresult`
  - 面向前端展示的最终诊断卡片数组。
  - 每个诊断包含：
    - `title`
    - `match`
    - `omim`
    - `gene`
    - `recommend`
    - `reason`
    - `pathogenesis`
    - `description`
    - `consultation`
    - `reference`
    - `treatment`

## 3. 对 yk-FERTA 的直接启发

### 3.1 HPO 人工确认应继续作为强制步骤

DeepRare 网页端最终使用的是干净的 HPO 集合：

- `HP:0032192`：葡萄胎
- `HP:0000789`：不孕

这说明产品化场景下不能完全依赖自动表型提取。尤其是不孕不育病例常常是低质量自然语言输入，必须允许医生确认、删除、补充 HPO。

yk-FERTA 当前已经加入 HPO 人工确认，这个方向应保留，并作为诊断前置强制步骤。

### 3.2 不用固定规则替代复杂临床复审

网页端 DeepRare 的一个重要设计哲学是：先通过工具、检索和病例库生成候选证据，再通过综合分析、疾病匹配和反思判断去筛选结果。它不是把所有判断写成固定规则。

这对 yk-FERTA 很重要。葡萄胎只是一个测试用例，不能因为一次 case searcher 混入了弱相关案例，就把系统改成大量葡萄胎专用规则。实际不孕不育场景会涉及：

- 女性因素
- 男性因素
- 胚胎/配子发育异常
- 复发性流产
- 葡萄胎和滋养细胞疾病
- DSD
- 染色体异常
- 单基因病和表型相关变异

这些场景的边界会交叉，固定规则很容易过拟合。更合理的工程策略是：

- 规则层负责证据分层、来源标注和明显错误过滤。
- 检索层尽量召回足够多的候选证据。
- LLM 综合层负责生成候选诊断。
- LLM 复审层负责判断每个候选病是否真正被证据支持。
- 最终输出必须展示支持证据、反对证据、缺失信息和建议检查。

因此，私有历史检测案例库不应被规则层直接解释为诊断依据，而应以 `testing_finding_reference` 的角色进入后续 LLM 复审，由复审模块判断其对候选病的支持强度。

### 3.3 临床诊断与分子确认必须分开

DeepRare 网页端对葡萄胎类 case 的最终呈现有一个值得借鉴的细节：它会把“临床诊断”和“可能亚型 / 推荐基因检测”分开，而不是把没有患者分子证据的病例直接写成“由某基因突变导致”。

yk-FERTA 需要沿用这个产品约束：

- 没有当前患者基因检测结果时，只能输出“临床疑似诊断”。
- NLRP7、KHDC3L、ACSF3 等基因可以作为疾病层面的“致病基因/相关基因”或“推荐检测目标”，但不能在没有患者本人检测结果时写成已确认病因。
- 不能仅凭表型、公共病例、私有历史检测案例，就说当前患者“由某基因突变导致”。
- 私有历史检测库只提供 `testing_finding_reference`，不能替代当前患者的分子确认。
- 反思阶段需要明确指出“临床证据支持什么”和“分子证据还缺什么”。

这不是针对某一个葡萄胎 case 的修补，而是面向产品安全性的通用规则：**分子病因需要患者本人的分子证据确认**。

### 3.4 过程透明比单次最终答案更重要

DeepRare 网页端持续推送每一步状态，并最终把每一步结果转成 artifact。这个设计对医生端很重要，因为医生需要知道：

- 哪些 HPO 被采用
- 哪些 API 给出候选病
- 哪些文献/网页/病例被检索到
- 哪些候选病被反思阶段排除
- 最终建议依据来自哪里

yk-FERTA 后续应把“可追踪 artifact”作为核心产品结构，而不是只优化最终 summary。

### 3.5 疾病复核阶段需要领域映射支持

DeepRare 网页端在葡萄胎 case 中最终能输出：

- 家族性复发性葡萄胎
- NLRP7/KHDC3L 相关型
- 妊娠滋养细胞疾病/肿瘤

这类结果不能完全依赖 Orphanet embedding。yk-FERTA 需要一个不孕不育领域 disease mapping 层，覆盖常见专病实体、基因、OMIM、别名和推荐检查。

## 4. 当前 yk-FERTA 与网页端差距

### 4.1 Phenotype Analyser 不够透明

当前本地实现已经接入 Phenobrain，并且能跑通。但 PubCaseFinder 和 HPO 官网检索存在问题：

- PubCaseFinder 当前测试返回 `404`，异常被静默吞掉。
- HPO 官网检索依赖 Selenium/chromedriver，当前环境不可用。
- 当前 artifact 中没有清楚展示每个工具的成功/失败状态。

### 4.2 Knowledge Searcher 不稳定

当前本地任务中仍出现过：

- DuckDuckGo 无结果
- PubMed 缺少依赖或检索失败
- arXiv 调用错误
- Wikipedia SSL 错误

这会导致后续 LLM 缺少可靠证据，或者把错误信息当成 evidence。

### 4.3 相似病例库尚不适合不孕不育场景

当前本地病例库主要来自 DeepRare 罕见病病例，针对不孕不育的相关性不足。对于葡萄胎 case，部分相似病例仍有噪声。

短期内应降低相似病例对最终诊断的权重；中期应构建不孕不育专病病例库。

### 4.4 最终输出 schema 还不够产品化

当前本地输出有 summary、candidate、review，但还没有完全对齐 DeepRare 网页端的诊断卡片结构。

后续应形成稳定的前端消费 schema，而不是依赖自由文本。

## 5. 后续开发优先级

### P0. 固化 HPO 人工确认流程

目标：

- 临床文本输入后，必须先进入 HPO 确认页。
- 用户可删除自动提取错误 HPO。
- 用户可从 CHPO/HPO 本地表搜索添加 HPO。
- 只有确认后的 HPO 才进入诊断主流程。

当前状态：

- 已实现基础版本。
- 应继续保留为强制步骤。

### P1. 重构 Phenotype Analyser artifact

目标：

把表型分析工具拆成明确的三路结果：

- `phenotype_tools.pubcasefinder`
- `phenotype_tools.phenobrain`
- `phenotype_tools.hpo_association`

每一路都应包含：

- `status`: `success | failed | skipped`
- `query`
- `raw_result`
- `parsed_candidates`
- `error`
- `elapsed_ms`

短期实现建议：

- Phenobrain 保持现有调用。
- PubCaseFinder 修复 API 或替换为稳定封装。
- HPO 官网检索不要继续强依赖 Selenium，优先考虑本地 HPO association 或可失败的轻量检索。

当前开发状态：

- 已新增 `phenotype_tools` artifact。
- 每个工具会记录 `status / query / raw_result / parsed_candidates / error / elapsed_ms`。
- 当前配置默认只启用 Phenobrain。
- PubCaseFinder 当前线上旧接口和文档接口均返回 404，暂时配置为 `skipped`。
- HPO 官网检索依赖 Selenium/chromedriver，暂时配置为 `skipped`。

### P2. 修复 Knowledge Searcher

目标：

让在线知识检索至少稳定提供：

- PubMed 摘要
- OMIM 页面/条目
- Orphanet 页面/本地知识
- 必要时的网页搜索结果

短期策略：

- 优先修 PubMed。
- OMIM/Orphanet 对候选病复核更重要，应优先于宽泛网页搜索。
- arXiv 对不孕不育临床诊断价值较低，可降级或暂时关闭。
- 不要把失败信息作为证据传给 LLM。

当前开发状态：

- 已修复 PubMed 主路径：不再依赖 DeepRare 原有 `PubMedRetriever` / `xmltodict` 链路，改为直接调用 NCBI E-utilities。
- 已增加 PubMed 轻量重排：对复发性葡萄胎场景优先提升 `NLRP7 / KHDC3L / recurrent hydatidiform mole / infertility` 相关文献。
- 已修复网页搜索主路径：优先使用维护中的 `ddgs` 包获取网页 snippet；旧版 `duckduckgo_search` 仅作为回退。
- 已把 arXiv / Wikipedia 默认关闭：当前价值低且 DeepRare 原始实现存在 handler 调用兼容问题。
- 已避免失败信息污染 evidence：无结果、依赖错误、调用异常不会再作为医学证据进入后续 LLM。

### P3. 建立不孕不育 disease mapping 层

目标：

建立本地结构化映射文件，覆盖：

- 疾病中文名/英文名
- 别名
- OMIM / Orphanet / HPO
- 相关基因
- 典型 HPO
- 推荐检查
- 推荐会诊科室
- 生育建议模板

首批应覆盖：

- 家族性复发性葡萄胎
- HYDM1 / NLRP7
- HYDM2 / KHDC3L
- HYDM3 / PADI6
- HYDM4 / MEI1 / TOP6BL / REC114
- 妊娠滋养细胞疾病
- 早发性卵巢功能不全
- 非梗阻性无精子症
- 精子发生障碍
- 复发性流产相关遗传病因

### P4. 输出 DeepRare 风格结构化结果

目标：

本地结果也输出以下 artifact：

- `result_hpo`
- `result_inforetrieval`
- `result_preliminarydiagnosis`
- `result_reflexion`
- `result_suggest`
- `diagnosisresult`

这样前端可以从调试页逐步升级为医生端产品页。

### P5. 引入 search-depth 自反思机制

目标：

当候选病匹配度低、证据不足或复核结果冲突时，自动扩大搜索深度。

触发条件示例：

- top diagnosis confidence 低于阈值
- 多数候选病复核为 unsupported
- disease normalizer 无法映射核心候选病
- evidence 数量不足

该机制应在 P1-P4 稳定后再做，否则会放大噪声。

## 6. 推荐近期执行顺序

1. 保留并完善 HPO 人工确认。
2. 重构 Phenotype Analyser，先把 Phenobrain / PubCaseFinder / HPO association 的状态暴露清楚。
3. 修 PubMed/OMIM/Orphanet 证据检索，减少宽泛网页依赖。
4. 建立不孕不育 disease mapping 文件。
5. 对齐 DeepRare 网页端的结构化 artifact 输出。
6. 最后再做 search-depth 多轮反思。

## 7. 一句话结论

DeepRare 网页端真正值得借鉴的不是某一个 API，而是：

**强制 HPO 确认 + 多源候选生成 + 证据检索 + 候选复核 + 结构化诊断卡片 + 全流程 SSE 可追踪。**

yk-FERTA 后续应沿这个产品化结构推进，但知识源和疾病映射必须换成不孕不育领域可控版本。
