# yk-FERTA 接口契约 v1.2（对接文档）

本文档用于 IT 集成对接，覆盖：

- REST API 契约
- SSE 事件流契约
- 示例请求/响应
- 错误处理模板与重试建议

> 版本：`v1.2`  
> 服务入口：`/api/v1`  
> 鉴权：当前无（后续由公司系统网关/平台层接入）

---

## 1. 总体时序

1. `POST /api/v1/cases` 创建病例
2. `POST /api/v1/tasks` 创建任务
3. `GET /api/v1/tasks/{task_id}/events` 订阅 SSE 进度
4. 任务完成后 `GET /api/v1/tasks/{task_id}/result`
5. 如需中间产物，调用 `GET /api/v1/tasks/{task_id}/artifacts`

补充边界：

- 正式前端主消费 `/api/v1/tasks/{task_id}/result`
- `/artifacts` 仅用于追溯、审计、研发调试
- 若需复刻工作流中间过程，可读取 `/artifacts`，但不应依赖其替代 `/result`

---

## 2. REST API

### 2.0 统一错误体

除 SSE 外，当前 REST API 统一返回以下错误结构：

```json
{
  "error_code": "TASK_NOT_FOUND",
  "message": "任务不存在",
  "retryable": false,
  "details": null
}
```

字段说明：

- `error_code`：稳定错误码，供前端和监控使用
- `message`：面向用户或开发的中文错误信息
- `retryable`：是否建议自动或手动重试
- `details`：可选调试信息

当前最常见错误码：

- `INVALID_REQUEST`
- `CASE_NOT_FOUND`
- `TASK_NOT_FOUND`
- `TASK_RESULT_NOT_READY`
- `ARTIFACT_NOT_FOUND`
- `IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD`
- `IDEMPOTENCY_RESOURCE_MISSING`

### 2.0.1 契约冻结范围

当前版本已冻结以下输出边界：

- `/api/v1/tasks/{task_id}`：任务快照
- `/api/v1/tasks/{task_id}/result`：正式结果接口
- `/api/v1/tasks/{task_id}/events`：SSE 进度事件

当前版本未冻结为正式前端强依赖的部分：

- artifact 内部细字段的长期兼容性
- 研发调试页面内部展示结构

## 2.1 健康检查

### GET `/healthz`

#### 200

```json
{
  "status": "ok"
}
```

---

## 2.2 HPO 相关

### POST `/api/v1/hpo/extract`

用于从病例 payload 提取 phenotype/HPO（由后端当前配置的 extractor 决定实现）。

#### Request

```json
{
  "patient_payload": {
    "patient_id": "case-demo-001",
    "chief_complaint": "不孕不育",
    "history_of_present_illness": "两次葡萄胎妊娠",
    "raw_note": "患者有不孕不育病史，既往发生两次葡萄胎妊娠..."
  }
}
```

说明：服务运行配置在部署时固定，不通过前端请求传入配置文件路径。

#### 200

```json
{
  "phenotypes": [
    {
      "label": "Hydatidiform mole",
      "code": "HP:0032192",
      "source": "rag-hpo-service",
      "confidence": 0.91,
      "notes": ""
    }
  ]
}
```

---

### GET `/api/v1/hpo/search?q={keyword}&limit={n}`

用于人工补充 HPO 时的候选搜索。

#### 200

```json
{
  "query": "葡萄胎",
  "hits": [
    {
      "code": "HP:0032192",
      "label": "Hydatidiform mole",
      "chinese_label": "葡萄胎",
      "source": "chpo-2025-4"
    }
  ]
}
```

---

## 2.3 病例

### POST `/api/v1/cases`

支持可选请求头：

- `Idempotency-Key: <client-generated-key>`

当同一个 key 搭配完全相同的请求体重复提交时，后端返回同一个 `case_id`。
当同一个 key 搭配不同请求体重复提交时，返回 `409`。

#### Request

```json
{
  "case_id": "case_custom_001",
  "source": "web-ui",
  "input_mode": "clinical_note",
  "patient_payload": {
    "patient_id": "case_custom_001",
    "chief_complaint": "不孕不育",
    "history_of_present_illness": "两次葡萄胎妊娠",
    "raw_note": "..."
  },
  "manual_phenotypes": [
    {
      "label": "Hydatidiform mole",
      "code": "HP:0032192",
      "source": "manual-review",
      "confidence": 1.0,
      "notes": ""
    }
  ]
}
```

#### 200

```json
{
  "case_id": "case_custom_001",
  "source": "web-ui",
  "input_mode": "clinical_note",
  "patient_payload": {
    "patient_id": "case_custom_001",
    "chief_complaint": "不孕不育",
    "history_of_present_illness": "两次葡萄胎妊娠",
    "raw_note": "..."
  },
  "manual_phenotypes": [
    {
      "label": "Hydatidiform mole",
      "code": "HP:0032192",
      "source": "manual-review",
      "confidence": 1.0,
      "notes": ""
    }
  ],
  "created_at": "2026-04-23T08:30:10.120000+00:00",
  "idempotency_key": "case-create-001"
}
```

---

### GET `/api/v1/cases/{case_id}`

#### 200

返回结构同 `POST /api/v1/cases` 响应。

#### 404

```json
{
  "error_code": "CASE_NOT_FOUND",
  "message": "病例不存在",
  "retryable": false,
  "details": null
}
```

---

## 2.4 任务

### POST `/api/v1/tasks`

支持可选请求头：

- `Idempotency-Key: <client-generated-key>`

当同一个 key 搭配完全相同的请求体重复提交时，后端返回同一个 `task_id`。
当同一个 key 搭配不同请求体重复提交时，返回 `409`。

#### Request

```json
{
  "case_id": "case_custom_001",
  "top_k": 5,
  "workflow_name": "clinical_mvp_v1"
}
```

#### 200

```json
{
  "task_id": "task_43b79d7c6f95",
  "case_id": "case_custom_001",
  "workflow_name": "clinical_mvp_v1",
  "status": "queued",
  "stage": "queued",
  "progress": 0,
  "search_depth": 1,
  "params": {
    "top_k": 5
  },
  "started_at": "",
  "finished_at": "",
  "error_message": null,
  "failure_type": null,
  "metrics": {},
  "idempotency_key": "task-create-001"
}
```

#### 404

```json
{
  "error_code": "CASE_NOT_FOUND",
  "message": "病例不存在，无法创建任务",
  "retryable": false,
  "details": null
}
```

---

### GET `/api/v1/tasks`

支持可选查询参数：`case_id`

#### 200

```json
{
  "tasks": [
    {
      "task_id": "task_43b79d7c6f95",
      "case_id": "case_custom_001",
      "workflow_name": "clinical_mvp_v1",
      "status": "completed",
      "stage": "completed",
      "progress": 100,
      "search_depth": 1,
      "params": {
        "top_k": 5
      },
      "started_at": "2026-04-23T08:31:01.100000+00:00",
      "finished_at": "2026-04-23T08:33:05.880000+00:00",
      "error_message": null,
      "failure_type": null,
      "metrics": {
        "stage_timings_ms": {
          "phenotype_extraction": 920,
          "phenotype_analysis": 4100,
          "parallel_diagnosis": 15000,
          "comprehensive_analysis": 11000,
          "disease_normalization": 180,
          "per_disease_verification": 24000,
          "final_synthesis": 8200
        },
        "total_duration_ms": 64000
      }
    }
  ]
}
```

---

### GET `/api/v1/tasks/{task_id}`

#### 200

返回单个 `TaskResponse`，结构同上。

#### 404

```json
{
  "error_code": "TASK_NOT_FOUND",
  "message": "任务不存在",
  "retryable": false,
  "details": null
}
```

---

### POST `/api/v1/tasks/{task_id}/cancel`

#### 200

返回取消后的 `TaskResponse`（`status` 可能为 `cancelled`）。

#### 404

```json
{
  "error_code": "TASK_NOT_FOUND",
  "message": "任务不存在",
  "retryable": false,
  "details": null
}
```

---

## 2.5 产物与结果

### GET `/api/v1/tasks/{task_id}/artifacts`

#### 200

```json
{
  "artifacts": [
    {
      "artifact_id": "artf_...",
      "task_id": "task_43b79d7c6f95",
      "artifact_type": "hpo",
      "version": 1,
      "data": {
        "phenotypes": []
      },
      "created_at": "2026-04-23T08:31:02.000000+00:00"
    }
  ]
}
```

#### 404

```json
{
  "error_code": "TASK_NOT_FOUND",
  "message": "任务不存在",
  "retryable": false,
  "details": null
}
```

---

### GET `/api/v1/tasks/{task_id}/artifacts/{artifact_type}`

#### 200

返回单个 `ArtifactResponse`。

#### 404

```json
{
  "error_code": "ARTIFACT_NOT_FOUND",
  "message": "指定产物不存在",
  "retryable": false,
  "details": {
    "artifact_type": "reviews"
  }
}
```

---

### GET `/api/v1/tasks/{task_id}/result`

该接口是正式前端优先依赖的主结果接口。

#### 200

```json
{
  "response": {
    "patient_id": "case_custom_001",
    "phenotypes": [],
    "phenotype_hints": [],
    "phenotype_tool_runs": [],
    "knowledge_evidence": [],
    "similar_cases": [],
    "initial_candidates": [],
    "normalized_candidates": [],
    "reviews": [],
    "final_recommendation": {
      "summary": "......",
      "candidates": [],
      "evidence": [],
      "reviews": [],
      "next_steps": [],
      "cautions": [],
      "diagnosis_cards": [
        {
          "disease_name_zh": "家族性复发性葡萄胎",
          "disease_name_en": "Recurrent hydatidiform mole",
          "clinical_diagnosis": "Recurrent hydatidiform mole",
          "support_level": "高",
          "confidence": 0.84,
          "omim_id": "231090",
          "omim_url": "https://www.omim.org/entry/231090",
          "orphanet_id": "ORPHA:99927",
          "orphanet_url": "http://www.orpha.net/consor/cgi-bin/OC_Exp.php?Expert=99927",
          "inheritance": "常染色体隐性",
          "disease_genes": ["NLRP7", "KHDC3L"],
          "molecular_mechanism": "母源印记调控异常相关分子机制（待患者分子结果确认）",
          "pathogenesis": "......",
          "specialties": ["生殖遗传", "妇科"],
          "supporting_evidence": [],
          "contradicting_evidence": [],
          "missing_evidence": [],
          "recommended_tests": [],
          "references": [],
          "cautions": []
        }
      ]
    },
    "stage_notes": {
      "entry_mode": "manual-phenotypes",
      "search_depth": "1"
    }
  },
  "timing": {
    "stage_timings_ms": {},
    "total_duration_ms": 64000
  }
}
```

`response` 顶层字段当前冻结为：

- `patient_id`
- `phenotypes`
- `phenotype_hints`
- `phenotype_tool_runs`
- `knowledge_evidence`
- `similar_cases`
- `initial_candidates`
- `normalized_candidates`
- `reviews`
- `final_recommendation`
- `stage_notes`

#### 404

```json
{
  "error_code": "TASK_RESULT_NOT_READY",
  "message": "任务结果尚未生成",
  "retryable": true,
  "details": null
}
```

`final_recommendation.diagnosis_cards` 当前作为前端冻结字段使用，卡片固定包含以下字段：

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

---

## 3. SSE 事件流契约

### GET `/api/v1/tasks/{task_id}/events`

`Content-Type: text/event-stream`

支持断线续传头：`Last-Event-ID`

### 3.1 普通事件格式

```text
id:12
data:{"task_id":"task_43b79d7c6f95","step":"comprehensive_analysis","task_stage":3,"seq_in_stage":4,"progress":60,"message":"综合诊断分析完成","ts_ms":1776671595051,"data":{"candidate_count":5}}
```

`data` 字段结构：

- `task_id`: 任务 ID
- `step`: 阶段步骤名（字符串）
- `task_stage`: 阶段编号（整型）
- `seq_in_stage`: 阶段内序号（整型）
- `progress`: 0-100
- `message`: 文本说明
- `ts_ms`: 毫秒时间戳
- `data`: 步骤附加数据（对象）

### 3.2 Keep-alive

```text
: keep-alive
```

### 3.3 终态事件（done）

```text
event:done
data:{"task_id":"task_43b79d7c6f95","step":"task_all_done","progress":100,"task_stage":999,"seq_in_stage":999,"message":"completed","ts_ms":1776671595999,"data":{}}
```

终态事件与普通事件使用同一数据结构，只是额外通过 `event:done` 表示流结束。

---

## 4. 错误语义与处理模板

## 4.1 HTTP 错误（同步接口）

当前标准错误体：

```json
{
  "error_code": "TASK_RESULT_NOT_READY",
  "message": "任务结果尚未生成",
  "retryable": true,
  "details": null
}
```

---

## 4.2 任务级错误（异步执行）

查询 `GET /api/v1/tasks/{task_id}` 时看：

- `status = failed`
- `error_message`
- `failure_type`

`failure_type` 当前枚举：

- `configuration_error`
- `upstream_error`
- `pipeline_error`

---

## 4.3 客户端错误处理模板（建议）

```text
if HTTP 404 and error_code == "TASK_RESULT_NOT_READY":
  继续订阅 SSE 或轮询 task 状态

if task.status == "failed":
  if failure_type == "upstream_error":
    允许用户发起重跑（新建 task）
  else:
    提示人工排查配置/流程错误

if SSE 中断:
  使用 Last-Event-ID 重连
```

---

## 5. 重试策略（建议）

- `POST /cases`、`POST /tasks`：支持 `Idempotency-Key`
- `GET /tasks/{id}`、`GET /result`、`GET /artifacts`：可安全重试
- SSE 断线：带 `Last-Event-ID` 重连
- 外部依赖抖动（上游 API）：建议在业务层触发“重建任务重跑”

---

## 6. 集成注意事项

1. 当前接口无鉴权，生产接入请通过公司网关层统一鉴权。
2. `result.response` 为主业务负载，`timing` 用于性能与排障。
3. artifact 用于追溯和研发调试，不应替代 `/result` 作为正式前端唯一数据源。
4. `diagnosis_cards` 中的 `disease_genes` 与 `molecular_mechanism` 是疾病知识，不等同于患者本人已检出分子异常。
5. 私有历史检测案例在推理中作为 `testing_finding_reference`，仅供参考，不作为确诊证据。

---

## 7. 版本策略

- 当前文档对应 `v1`。
- 当前交付冻结版本为 `v1.2`。
- 后续如字段变更，建议采用：
  - 路径升级：`/api/v2/...`，或
  - 响应体加 `schema_version`（向后兼容）
