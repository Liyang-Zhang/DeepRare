# yk-FERTA 产品化落地步骤（面向智能体内核交付）

本文档把以下交付目标拆解为可执行步骤：

- 一个可嵌入公司系统的“智能体推理服务”
- 一套稳定的接口契约（字段、状态、错误语义）
- 一套可运行的最小演示前端（非最终产品 UI）

边界约束：不包含账号/权限/历史中心/平台监控等 IT 平台职责。

---

## 0. 当前状态快照（基于现代码）

已具备：

- 异步任务服务（FastAPI + SQLite）
- REST + SSE 能力
- 多阶段推理链路（含 HPO 确认、候选生成、逐病复核、最终综合）
- 结构化 `result` 与 `artifacts`
- 调试前端页面（`/debug/task-console`）

主要缺口：

- 对 IT 可交付的“接口契约 v1 文档”还未正式冻结
- 错误语义尚未统一成外部对接友好的错误码表
- 演示前端仍偏研发控制台，不是“最小业务演示页”

---

## 1. 里程碑 M1：推理服务内核冻结

目标：把“能跑”变成“稳定可交付”。

### 1.1 冻结流程与产物

1. 固定阶段顺序与语义：  
`case_ingestion -> phenotype_extraction -> phenotype_analysis -> parallel_diagnosis -> comprehensive_analysis -> disease_normalization -> per_disease_verification -> final_synthesis -> completed`
2. 固定 artifact 类型（至少）：
- `hpo`
- `phenotype_hints`
- `phenotype_tools`
- `evidence`
- `similar_cases`
- `preliminary_diagnosis`
- `normalized_candidates`
- `candidate_evidence`
- `reviews`
- `final_recommendation`
- `result`

### 1.2 冻结核心输出字段

1. 冻结 `GET /api/v1/tasks/{task_id}/result` 返回结构。
2. 冻结 `response.final_recommendation.diagnosis_cards` 字段（尤其）：
- `disease_name_zh`, `disease_name_en`, `clinical_diagnosis`
- `support_level`, `confidence`
- `omim_id`, `omim_url`, `orphanet_id`, `orphanet_url`
- `inheritance`, `disease_genes`, `molecular_mechanism`, `pathogenesis`
- `supporting_evidence`, `contradicting_evidence`, `missing_evidence`
- `recommended_tests`, `specialties`, `references`, `cautions`

### 1.3 验收标准

- 同一配置下重复运行，接口字段稳定不漂移
- 外部检索失败时任务可降级完成（非致命不 fail）
- 能稳定产出 `result` + 全流程可追溯 artifacts

---

## 2. 里程碑 M2：接口契约 v1 定版（给 IT 对接）

目标：让 IT 可以在不读源码情况下完成集成。

### 2.1 输出接口清单

1. `POST /api/v1/cases`
2. `GET /api/v1/cases/{case_id}`
3. `POST /api/v1/tasks`
4. `GET /api/v1/tasks`
5. `GET /api/v1/tasks/{task_id}`
6. `POST /api/v1/tasks/{task_id}/cancel`
7. `GET /api/v1/tasks/{task_id}/events`
8. `GET /api/v1/tasks/{task_id}/artifacts`
9. `GET /api/v1/tasks/{task_id}/artifacts/{artifact_type}`
10. `GET /api/v1/tasks/{task_id}/result`
11. `POST /api/v1/hpo/extract`
12. `GET /api/v1/hpo/search`

### 2.2 SSE 协议冻结

1. 固定 event payload 字段：
- `task_id`
- `step`
- `task_stage`
- `seq_in_stage`
- `progress`
- `message`
- `ts_ms`
- `data`
2. 固定终态事件：`event:done` + `step=task_all_done`

### 2.3 错误语义冻结

1. HTTP 错误（当前）：
- `404 case not found`
- `404 task not found`
- `404 artifact not found`
- `404 result not available`
2. 任务失败分类（`TaskResponse.failure_type`）：
- `configuration_error`
- `upstream_error`
- `pipeline_error`
3. 对接重试建议：
- 接口 5xx 或网络抖动：客户端指数退避重试
- 任务 `failed` 且 `failure_type=upstream_error`：允许重建任务重跑
- `result not available`：继续订阅 SSE 或轮询 task 状态

### 2.4 验收标准

- 提供 v1 对接文档（路径、字段、示例、错误语义、重试策略）
- 提供 3 个样例：
- 正常完成
- 部分降级但完成
- 失败任务

---

## 3. 里程碑 M3：最小演示前端（非最终 UI）

目标：做“可看可验”的产品化演示壳。

### 3.1 页面拆分

1. 页面 A：病例输入与任务发起
- 临床信息录入
- HPO 提取与人工确认
- 创建 case + task
2. 页面 B：任务与诊断结果
- SSE 实时进度
- 分阶段结果卡
- 最终诊断卡（含证据、缺失证据、建议检查）

### 3.2 展示规范（必须）

1. 中文为主展示
2. 证据可追溯（来源、URL、引用）
3. 明确区分：
- 临床诊断支持
- 疾病层面的基因/机制知识
- 患者本人是否已有分子证据

### 3.3 验收标准

- 前端只依赖公开 API 契约，不读取内部调试对象
- 任一任务可从页面完整回放阶段链路
- 结果卡可被医生快速审阅（支持/反对/缺失证据清晰）

---

## 4. 里程碑 M4：交付包

目标：可移交 IT 集成，智能体团队职责闭环。

### 4.1 交付物清单

1. 服务代码与运行说明
2. 接口契约 v1 文档
3. 字段字典与 JSON 示例
4. 错误语义与重试指南
5. 最小演示前端页面
6. 已知限制与非目标清单

### 4.2 验收口径

1. 能独立部署并运行完整任务
2. 接口字段稳定且有版本边界
3. 任务全流程可追溯
4. 与 IT 边界清晰，不混入平台职责

---

## 5. 建议执行顺序（从今天起）

1. 本周先完成 M2（接口契约 v1 文档冻结）
2. 下周完成 M3（调试页重构为最小演示前端）
3. 然后出 M4 交付包并组织评审

这样可以最快把当前“研发原型”升级为“可集成交付件”。

