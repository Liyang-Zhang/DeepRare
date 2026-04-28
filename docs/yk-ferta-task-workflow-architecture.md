# yk-FERTA 任务流与流式架构设计

本文档基于三部分信息整理：

- `DeepRare` 源码与工作流分析
- 对 `deeprare.cn` 网页端真实交互的抓包观察
- 当前 `yk-FERTA` 已实现的临床信息版 MVP pipeline

目标不是复刻 `DeepRare` 的接口名字，而是提炼出可在 `yk-FERTA` 中落地的后端架构：  
**Task + Workflow + Artifact + SSE**

---

## 1. 结论先行

`DeepRare` 网页版真正值得借鉴的核心，不是某一条 prompt，而是：

1. 把一次诊断请求建模为一个长期运行的 `task`
2. 把中间结果建模为结构化 `artifact`
3. 把执行过程建模为标准化 `event`
4. 用 `HTTP + SSE` 混合架构承载“提交任务 + 查看过程 + 获取结果”
5. 用阶段状态机管理复杂医疗 workflow，而不是一次同步返回全部文本

补充一条更重要的工程原则：

6. 不把复杂临床判断固化为单个测试用例驱动的规则，而是用“结构化召回 + LLM 复审 + 反思判断”的架构承载复杂性

对 `yk-FERTA` 而言，这意味着下一阶段的重点应该从：

- “继续补一个工具”

转到：

- “把现有 pipeline 封装成可管理的任务系统”

---

## 1.1 诊断推理设计原则

`yk-FERTA` 后续开发需要明确区分两类逻辑：

- 工程规则：用于输入校验、证据分层、召回范围控制、数据源标注、明显错误过滤。
- 临床推理：用于判断证据是否真正支持某个候选诊断、是否存在反证、哪些结果只是弱相关。

不孕不育场景的病例复杂度高，真实输入也会非常不稳定。不能因为某个测试用例表现不好，就不断追加针对单病种、单关键词的硬编码规则。这样短期可能改善一个 demo，但会降低系统在真实场景中的泛化能力。

更合理的方向是：

1. 规则层只负责把数据整理成可被推理的结构化证据。
2. 召回层尽量保证相关证据不漏掉，同时保留来源、角色和置信信号。
3. LLM 综合层负责把多源证据合并成候选诊断。
4. LLM 复审层逐个候选病判断“证据是否足够成立”。
5. 反思层明确列出支持证据、反对证据、缺失信息和下一步检查。

当前本地相似案例库也应遵循这个原则：

- 公共病例库可以作为 `diagnosis_reference`。
- 私有历史检测库只能作为 `testing_finding_reference`。
- 固定规则只能帮助区分证据角色和基础相关性，不能替代候选病复核。
- 复杂的相关性判断，例如“这个相似检测案例是否真正支持 NLRP7/KHDC3L 相关复发性葡萄胎”，应交给 LLM 复审模块在明确证据边界下处理。

另一个必须长期遵守的产品级约束是：**临床疑似诊断和分子确认诊断必须分开表达**。

- 如果当前病例没有患者本人的基因检测结果，系统不能把疾病表述为“由某基因突变导致”。
- 可以输出“临床疑似某疾病 / 某综合征”，也可以提示“NLRP7、KHDC3L 等是该疾病相关基因或推荐检测目标”，但不能在没有患者本人检测结果时写成已确认分子病因。
- 基因只能作为“可能病因、推荐检测目标、待确认亚型”，不能作为已确认诊断依据。
- 私有历史检测案例中的基因命中只能说明“历史上有相似表型和相关变异检出”，不能替代当前患者的分子证据。
- 复审阶段需要显式指出：哪些证据支持临床诊断，哪些分子证据缺失，下一步应做什么检测确认。

这个原则来自 DeepRare 网页端的一个产品化处理：最终卡片会把“临床诊断”与“可能亚型 / 推荐基因检测”分开呈现，而不是直接把表型匹配结果写成已确认的基因病因。

这也是 `DeepRare` 值得借鉴的核心设计哲学：  
**不要用一个大 prompt 或一堆硬规则包办全部逻辑，而是把诊断拆成多个可追踪阶段，让 LLM 在合适的阶段承担推理和反思。**

---

## 2. 从真实 event stream 提炼出的运行模型

根据抓到的实际 SSE 事件，`DeepRare` 后端运行模型有几个非常明确的特征。

### 2.1 它是任务驱动，而不是同步调用

所有后续接口围绕 `task_id` 组织。

示例：

- `POST /api/v1/inputcase`
- `POST /api/v1/analysishpo`
- `POST /sse/v1/stream/dreason/{task_id}`
- `GET /api/v1/status`

这说明一次病例分析不是“请求 -> 直接返回最终报告”，而是：

1. 创建任务
2. 后台异步执行
3. 前端通过 SSE 持续消费事件
4. 任务完成后再读取最终结果

### 2.2 它有明确的阶段状态机

在真实 stream 中，可以看到典型阶段：

- `parallel_complete`
- `comprehensive_analysis`
- `disease_matching`
- `final_comprehensive_diagnosis`
- `result_hpo`
- `result_inforetrieval`
- `result_preliminarydiagnosis`
- `result_reflexion`
- `result_suggest`
- `diagnosisresult`
- `completed`
- `task_all_done`

这说明后端不是单一 LLM 连续输出，而是：

- 先跑若干内部步骤
- 每个阶段产出中间结果
- 最终聚合为面向用户的报告

### 2.3 它存在“并行阶段”和“聚合阶段”

从以下字段可以判断：

- `parallel_complete`
- `results.llm_response`
- `results.web_diagnosis`
- `results.similar_case_detailed`

说明系统在一个阶段中至少并发了多条子链：

- LLM case-only / zero-shot 诊断
- 网络搜索 / 文献搜索
- 相似病例检索

然后再进入：

- `comprehensive_analysis`

做统一综合。

### 2.4 它的最终结果不是单一文本，而是多块结构化结果

从 `result_hpo`、`result_inforetrieval`、`result_preliminarydiagnosis`、`result_reflexion`、`result_suggest`、`diagnosisresult` 可以看出，最终结果页本质上由多组 artifact 组成：

- HPO / 表型分析
- 信息检索摘要
- 初步诊断
- 反思判断
- 最终建议
- 候选疾病详情

这和我们当前 CLI 中已有的阶段结构非常接近，只是现在还没有服务端 artifact 抽象。

---

## 3. 对 yk-FERTA 的架构启示

这份真实 stream 对我们最大的启发，不是“DeepRare 某一步用了什么模型”，而是：

### 3.1 现有 pipeline 应继续保留

我们当前已经实现的阶段：

1. `Phenotype Extractor`
2. `Phenotype Analyser`
3. `Knowledge Searcher`
4. `Case Searcher`
5. `Initial Diagnosis Synthesis`
6. `Disease Normalizer`
7. `Per-Disease Verification`
8. `Final Diagnosis Synthesis`

这条链路本身没有问题，应该继续作为核心 workflow。

### 3.2 需要补的不是“更多 prompt”，而是运行时外壳

当前 `yk-FERTA` 已有：

- pipeline
- schema
- CLI
- 可替换 service

但还缺：

- task manager
- event store
- artifact store
- state machine
- SSE layer
- 结果恢复与重放

所以后续不应把主要精力放在“把 CLI 再堆复杂一点”，而应开始搭一个服务端执行壳。

---

## 4. 推荐的 yk-FERTA 核心对象模型

建议统一抽象四类核心对象。

### 4.1 Case

表示一次临床输入本身。

建议字段：

```json
{
  "case_id": "case_20260420_0001",
  "source": "doctor_ui",
  "input_mode": "clinical_note | phenotype_first",
  "patient_payload": {
    "chief_complaint": "...",
    "present_illness": "...",
    "history": "...",
    "laboratory_findings": "...",
    "imaging_findings": "...",
    "raw_note": "..."
  },
  "manual_phenotypes": [],
  "created_at": "2026-04-20T16:00:00+08:00"
}
```

### 4.2 Task

表示一次 workflow 执行。

建议字段：

```json
{
  "task_id": "task_20260420_0001",
  "case_id": "case_20260420_0001",
  "workflow_name": "clinical_mvp_v1",
  "status": "queued | running | completed | failed | cancelled",
  "stage": "knowledge_search",
  "progress": 55,
  "search_depth": 1,
  "started_at": "...",
  "finished_at": "...",
  "error_message": null
}
```

### 4.3 Event

表示任务执行中的每一次阶段通知。

建议字段：

```json
{
  "event_id": 18,
  "task_id": "task_20260420_0001",
  "step": "final_comprehensive_diagnosis",
  "task_stage": 4,
  "seq_in_stage": 18,
  "progress": 92,
  "message": "最终综合诊断生成完成",
  "ts_ms": 1776671703469,
  "payload": {}
}
```

### 4.4 Artifact

表示阶段产物。

建议字段：

```json
{
  "artifact_id": "art_001",
  "task_id": "task_20260420_0001",
  "artifact_type": "hpo | evidence | similar_cases | preliminary_diagnosis | review | final_report",
  "version": 1,
  "data": {},
  "created_at": "..."
}
```

---

## 5. 推荐的状态机设计

结合 `DeepRare` 真实 stream 和 `yk-FERTA` 现有 pipeline，建议统一成如下状态机。

### 5.1 顶层状态

```text
queued
running
completed
failed
cancelled
```

### 5.2 运行中阶段

```text
case_ingestion
phenotype_extraction
phenotype_analysis
parallel_diagnosis
comprehensive_analysis
disease_normalization
per_disease_verification
final_synthesis
result_packaging
```

### 5.3 与 DeepRare stream 的映射

建议映射如下：

| DeepRare 事件 | yk-FERTA 阶段 |
|---|---|
| `inputcase` | `case_ingestion` |
| `analysishpo` | `phenotype_extraction` |
| `parallel_running` | `parallel_diagnosis` |
| `parallel_complete` | `parallel_diagnosis` 完成 |
| `comprehensive_analysis` | `comprehensive_analysis` |
| `disease_matching` | `disease_normalization` + `per_disease_verification` |
| `final_comprehensive_diagnosis` | `final_synthesis` |
| `result_hpo` | `result_packaging` 中的 `artifact:hpo` |
| `result_inforetrieval` | `artifact:evidence_summary` |
| `result_preliminarydiagnosis` | `artifact:preliminary_diagnosis` |
| `result_reflexion` | `artifact:review_summary` |
| `result_suggest` | `artifact:clinical_suggestion` |
| `diagnosisresult` | `artifact:diagnosis_cards` |
| `completed` / `task_all_done` | `completed` |

这样做的好处是：

- 内部阶段更稳定
- 外部事件名可以调整
- 前端展示层可以继续做更细的页面模块拆分

---

## 6. 推荐的后端模块划分

建议拆成六层。

### 6.1 API Layer

职责：

- 接收病例提交
- 查询任务状态
- 返回 artifact
- 建立 SSE 连接

建议技术：

- `FastAPI`

### 6.2 Task Manager

职责：

- 创建 task
- 更新 task 状态
- 写入阶段事件
- 支持取消、重试、恢复

### 6.3 Workflow Runner

职责：

- 驱动现有 `ClinicalMvpPipeline`
- 在每个阶段前后发事件
- 持久化阶段 artifact

这是 `yk-FERTA` 最重要的连接层。

### 6.4 Artifact Store

职责：

- 按 `task_id + artifact_type` 持久化中间结果
- 支持后续前端分块展示
- 支持研发调试和回放

### 6.5 Event Stream Layer

职责：

- 从事件存储中读取并向前端流式推送
- 处理断线重连、`Last-Event-ID`
- 在流结束前持续心跳

### 6.6 Domain Services

即现有业务能力层：

- phenotype extractor
- phenotype analyser
- knowledge searcher
- case searcher
- initial synthesis
- normalizer
- verifier
- final synthesis

这一层尽量保持和服务运行框架解耦。

---

## 7. 推荐 API 设计

建议不要模仿 `DeepRare` 的原始接口名，而采用资源化设计。

### 7.1 Case API

```http
POST   /api/v1/cases
GET    /api/v1/cases/{case_id}
```

### 7.2 Task API

```http
POST   /api/v1/tasks
GET    /api/v1/tasks/{task_id}
GET    /api/v1/tasks?case_id={case_id}
POST   /api/v1/tasks/{task_id}/cancel
POST   /api/v1/tasks/{task_id}/retry
```

### 7.3 Event API

```http
GET    /api/v1/tasks/{task_id}/events
```

响应类型：

```http
Content-Type: text/event-stream
```

### 7.4 Artifact API

```http
GET    /api/v1/tasks/{task_id}/artifacts
GET    /api/v1/tasks/{task_id}/artifacts/hpo
GET    /api/v1/tasks/{task_id}/artifacts/evidence
GET    /api/v1/tasks/{task_id}/artifacts/similar_cases
GET    /api/v1/tasks/{task_id}/artifacts/preliminary_diagnosis
GET    /api/v1/tasks/{task_id}/artifacts/reviews
GET    /api/v1/tasks/{task_id}/artifacts/final_report
```

### 7.5 Result API

```http
GET    /api/v1/tasks/{task_id}/result
```

返回聚合后的最终结果对象。

---

## 8. 推荐的 SSE 事件 schema

建议直接兼容你抓到的风格，但规范字段。

### 8.1 基础事件

```json
{
  "task_id": "task_20260420_0001",
  "step": "comprehensive_analysis",
  "message": "正在进行综合诊断分析",
  "progress": 55,
  "task_stage": 3,
  "seq_in_stage": 9,
  "ts_ms": 1776671552093,
  "data": {}
}
```

### 8.2 阶段完成事件

```json
{
  "task_id": "task_20260420_0001",
  "step": "parallel_complete",
  "message": "并行诊断任务全部完成",
  "progress": 50,
  "task_stage": 3,
  "seq_in_stage": 8,
  "data": {
    "summary": "并行诊断分析完成，网络搜索结果：5条，LLM诊断：已完成，相似病例：已找到"
  },
  "results": {
    "llm_response": "...",
    "web_diagnosis": "...",
    "similar_case_detailed": "..."
  }
}
```

### 8.3 最终完成事件

```json
{
  "task_id": "task_20260420_0001",
  "step": "completed",
  "message": "AI诊断推理分析完成",
  "progress": 100,
  "complete": true,
  "result": {
    "artifact_types": [
      "hpo",
      "evidence",
      "preliminary_diagnosis",
      "review",
      "final_report"
    ]
  }
}
```

### 8.4 前端处理原则

前端收到 SSE 后，不要只做文本滚动，而是：

- 更新顶部进度条
- 更新当前阶段文案
- 将特定 `step_type` 映射到页面卡片
- 在任务完成后按 artifact 拉取完整结构化结果

也就是说：

- SSE 用于“过程感知”
- Artifact API 用于“结果展示”

不要把所有最终内容都塞进 SSE。

---

## 9. Artifact 设计建议

结合 `DeepRare` stream，建议第一版至少定义以下 artifact。

### 9.1 `hpo`

包含：

- 提取的 phenotype/HPO
- phenotype 组合分析
- phenotype 到候选病匹配摘要

### 9.2 `evidence`

包含：

- 文献摘要
- 指南/知识库摘要
- 类似病例摘要
- 诊断标准摘要

### 9.3 `preliminary_diagnosis`

包含：

- top-k 初步候选
- 每个候选的支持点与反证点
- 初步概率

### 9.4 `review`

包含：

- 每个候选病的逐病复核
- `support / contradict / uncertain`
- 复核理由

### 9.5 `final_report`

包含：

- 最终 top-k 结果
- 诊断理由
- 推荐检查
- 治疗建议
- 会诊建议
- 参考文献

### 9.6 `diagnosis_cards`

包含：

- 每个候选疾病的结构化详情
- 基因
- OMIM/Orphanet
- 诊断依据
- 治疗方案

这类 artifact 最适合前端卡片式展示。

---

## 10. 对当前 yk-FERTA 代码的落地方案

当前代码基础已经够用，可以按最小改造路径推进。

### 10.1 当前可复用部分

现有可以直接复用：

- `src/yk_ferta/pipelines/clinical_mvp.py`
- `src/yk_ferta/services/clinical_mvp.py`
- `src/yk_ferta/schemas/clinical.py`
- `src/yk_ferta/schemas/evidence.py`
- `src/yk_ferta/schemas/mvp.py`
- `src/yk_ferta/cli.py`

它们已经提供了：

- 明确的阶段顺序
- 结构化 schema
- 可替换的 service 组件

### 10.2 需要新增的模块

建议新增：

- `src/yk_ferta/tasking/models.py`
- `src/yk_ferta/tasking/store.py`
- `src/yk_ferta/tasking/events.py`
- `src/yk_ferta/tasking/runner.py`
- `src/yk_ferta/api/app.py`

建议职责：

#### `tasking/models.py`

定义：

- `CaseRecord`
- `TaskRecord`
- `TaskEvent`
- `TaskArtifact`

#### `tasking/store.py`

第一阶段先用本地持久化：

- SQLite
- 或 JSONL + 本地文件目录

建议优先 SQLite，原因是后面查询方便。

#### `tasking/events.py`

定义统一事件 schema 和事件工厂。

#### `tasking/runner.py`

负责：

- 调用现有 pipeline
- 在阶段边界发事件
- 把阶段结果写成 artifact

#### `api/app.py`

使用 `FastAPI` 暴露：

- case API
- task API
- artifact API
- SSE API

---

## 11. 推荐实现阶段

建议按四步推进。

### Phase 1：任务化现有 pipeline

目标：

- 不改业务逻辑
- 先把当前 pipeline 包装进 task runner

交付：

- 可以创建 task
- 可以查状态
- 可以持久化 artifact

### Phase 2：加 SSE

目标：

- 前端或命令行可以实时看到阶段进度

交付：

- `GET /tasks/{id}/events`
- 阶段事件流

### Phase 3：加结构化结果 API

目标：

- 不依赖“最终大文本”
- 支持前端分块展示

交付：

- `GET /tasks/{id}/artifacts/*`

### Phase 4：替换领域能力

目标：

- 用不孕不育场景的真实知识源替换通用能力

替换顺序建议：

1. 知识源
2. 相似病例库
3. 诊断综合 prompt
4. verification 逻辑

---

## 12. 对不孕不育项目的直接建议

对于 `yk-FERTA`，最应该借鉴 `DeepRare` 网页端的不是：

- 前端长什么样
- 某个接口叫什么
- 某段输出文案怎么组织

而是以下四点：

### 12.1 把长链路推理做成任务

不孕不育场景后面也会有：

- 结构化问诊
- 表型标准化
- 指南检索
- 病例检索
- 候选病因排序
- 检查建议
- 产品推荐

这些都不适合同步接口一口气返回。

### 12.2 把中间结果做成 artifact

因为医生端非常需要看到：

- 你识别了哪些关键信息
- 哪些证据支持哪个候选
- 为什么排除另一个候选

### 12.3 用 SSE 告知“阶段推进”，不是只流 token

前端展示应优先让用户知道：

- 现在在抽表型
- 现在在查证据
- 现在在匹配病例
- 现在在做综合判断

### 12.4 把最终结果页拆成多个结果卡片

从你抓到的 `DeepRare` 结果结构看，用户体验上最有效的是：

- HPO / 关键特征
- 资料检索
- 初步诊断
- 反思判断
- 最终建议
- 候选诊断卡片

这套结果组织方式完全值得借鉴。

---

## 13. 一句话总结

`yk-FERTA` 下一阶段不应只继续补算法模块，而应开始从“可跑的 pipeline”升级到“可管理的任务系统”。

最值得复刻的目标架构是：

**Clinical Pipeline + Task Manager + Artifact Store + SSE + Structured Result API**

这是从 `DeepRare` 研究流程走向产品化落地的关键一步。
