# yk-FERTA 本地诊断工作流汇总（2026-04-23）

本文档汇总当前 `yk-FERTA` 本地服务的实际诊断工作流，聚焦「从临床输入到最终可追溯诊断输出」的完整链路，作为当前阶段的对内开发基线。

## 1. 目标与边界

当前本地版目标是：

- 跑通 DeepRare 风格的多阶段诊断链路。
- 强制保留 HPO 人工确认。
- 输出可追溯中间产物（artifacts）和最终结构化诊断结果。

当前边界是：

- 仅覆盖“临床信息输入”主链路（不含 VCF/变异联合分析）。
- 用于研发与流程验证，不替代临床最终诊断。

## 2. 本地服务形态

当前为单服务形态（FastAPI + SQLite + 本地静态调试页）：

- API 入口：`src/yk_ferta/api/app.py`
- 任务状态机：`src/yk_ferta/tasking/stages.py`
- 主流程实现：`src/yk_ferta/services/clinical_mvp.py`
- 本地任务存储：`data/yk_ferta.sqlite3`
- 调试页：`/debug/task-console`（`src/yk_ferta/api/static/task_console.html`）

核心 API：

1. `POST /api/v1/cases` 创建病例
2. `POST /api/v1/tasks` 创建任务并异步执行
3. `GET /api/v1/tasks/{task_id}/events` 订阅 SSE 事件流
4. `GET /api/v1/tasks/{task_id}/result` 获取最终结果
5. `GET /api/v1/tasks/{task_id}/artifacts` 查看中间产物
6. `POST /api/v1/hpo/extract` 与 `GET /api/v1/hpo/search` 支持 HPO 提取与补充

## 3. 端到端工作流（本地实际执行）

### Step 0：病例输入与 HPO 确认（前置）

输入：

- 临床文本（主诉、现病史、既往史、原始病历等）

处理：

1. 调用 HPO 抽取（可走 RAG-HPO provider）
2. 用户在前端确认 HPO：删除误提取项、补充缺失项（含 CHPO/HPO 搜索）
3. 仅确认后的 HPO 进入主诊断流程

输出：

- `phenotypes`（已确认 HPO 集合）

### Step 1：Phenotype Analysis

输入：

- 已确认 HPO

处理：

- 调用 phenotype tools 生成候选疾病线索（当前实现可用/可配置启停）

输出：

- `phenotype_hints`
- `phenotype_tool_runs`（每个工具的状态、错误、候选条数）

### Step 2：Parallel Evidence Retrieval

输入：

- 病例文本
- 已确认 HPO

处理（并行概念层）：

- Knowledge Searcher：在线医学证据检索（网页/PubMed 等）
- Case Searcher：本地双库相似病例检索（公共病例库 + 私有检测案例库）

输出：

- `knowledge_evidence`
- `similar_cases`

### Step 3：First-Round Diagnosis Synthesis

输入：

- phenotype 工具候选
- 在线证据
- 相似病例
- 病例本身

处理：

- LLM 生成第一轮候选诊断（Top-K）

输出：

- `initial_candidates`

### Step 4：Disease Normalization

输入：

- 第一轮候选疾病名

处理：

- 本地映射/相似匹配归一到标准疾病实体（Orphanet/OMIM 优先，未命中则保留 unmapped）

输出：

- `normalized_candidates`

### Step 5：Per-Disease Verification

输入：

- 标准化后的候选病
- 病例/HPO
- 既有 evidence / similar cases

处理：

- 候选病逐个复核（支持证据、反证、缺失证据）
- 补充候选病级证据（candidate-scoped evidence）

输出：

- `reviews`
- `candidate_evidence`（若有）

### Step 6：Final Diagnosis Synthesis

输入：

- 初诊候选
- 标准化结果
- 逐病复核结果
- 全部证据

处理：

- LLM 综合输出中文为主的最终诊断摘要与结构化诊断卡

输出：

- `final_recommendation`
- `result`（完整结构化返回）

## 4. 结果结构（当前版本）

`result` 主要包含：

- `phenotypes`
- `phenotype_hints`
- `phenotype_tool_runs`
- `knowledge_evidence`
- `similar_cases`
- `initial_candidates`
- `normalized_candidates`
- `reviews`
- `final_recommendation`
- `timing`

其中 `final_recommendation` 已是前端可消费的产品结构，含：

- `summary`
- `diagnosis_cards`
- `next_steps`
- `cautions`

`diagnosis_cards` 关键字段（当前）：

- 疾病展示：`disease_name_zh`, `disease_name_en`, `clinical_diagnosis`
- 标识映射：`omim_id`, `omim_url`, `orphanet_id`, `orphanet_url`
- 疾病知识：`inheritance`, `disease_genes`, `molecular_mechanism`, `pathogenesis`
- 证据结构：`supporting_evidence`, `contradicting_evidence`, `missing_evidence`, `references`
- 临床建议：`recommended_tests`, `specialties`, `cautions`

## 5. 关键产品约束（已落实）

### 5.1 HPO 人工确认为强制前置

- 先提取、再人工确认，避免噪声 HPO 直接污染后续推理。

### 5.2 临床诊断与分子确认分离

- 无患者本人分子检测结果时，不可写成“由某基因导致”。
- 输出的是疾病层面的“致病基因/机制知识”，不是患者已确认分子病因。
- 当前产品语义已移除“分子亚型”主展示概念，改为：
  - `disease_genes`（致病基因/区域）
  - `molecular_mechanism`（分子/遗传机制）

### 5.3 私有案例库角色受限

- 私有历史检测案例按 `testing_finding_reference` 使用。
- 仅支持“表型-基因-检测经验”提示，不直接等价于确诊病例证据。

## 6. 本地知识与病例资源组织（当前）

### 6.1 本地病例检索

- 公共病例库：RDS 筛选后的不孕不育相关病例集
- 私有检测案例库：2025 历史检测项目清洗集（含 HPO 补充版）
- 向量检索：已构建本地向量索引与 rerank 流程

### 6.2 在线补充证据

- 作为辅助证据层，不替代本地可控知识层
- 检索失败时流程可降级继续执行，保证任务完整闭环

## 7. 可追溯能力（当前）

当前支持两类追溯：

1. 事件追溯（SSE）：
- 每阶段有 `step / progress / message / data`
- 可在前端实时展示“进行中 -> 完成”过程

2. 产物追溯（Artifacts）：
- 每阶段关键中间结果都可按 task 查询
- 可用于质控、回放和问题定位

## 8. 当前可作为“雏形完成”的判断标准

满足以下条件即可认为本地工作流雏形完成：

1. 能从临床文本出发跑通完整任务闭环
2. HPO 确认后进入多阶段推理链路
3. 可输出结构化诊断卡与证据分层
4. 可通过 SSE + artifacts 追踪全过程
5. 关键产品语义符合临床场景（尤其分子证据边界）

当前版本已满足以上 5 条，进入下一阶段应以“知识质量、证据质量、前端可审核体验和专病映射精度”作为优化重点。

