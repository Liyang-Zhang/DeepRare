# yk-FERTA 容器化设计说明（2026-04-29）

本文档只讨论一件事：如何把当前 yk-FERTA 工作流收口成一个可交付的 Docker 服务。

不讨论最终部署位置，不讨论鉴权、网关和正式前端。

## 1. 容器化目标

目标不是把整个研发目录原样塞进镜像，而是交付一个职责单一的“核心智能体服务容器”：

- 提供病例创建、任务执行、SSE 进度、结果与追溯接口
- 提供演示页与 Swagger UI，便于联调和预验收
- 通过挂载配置和数据目录，在不同环境下复用同一镜像

推荐形态：

- `yk-ferta-core`：当前主服务容器
- `rag-hpo`：单独服务，不与 `yk-ferta-core` 打包进同一镜像
- `nginx`：由部署侧决定是否使用，非 yk-FERTA 镜像内职责

## 2. 推荐的容器边界

### 2.1 应写入镜像的内容

这些内容属于“程序本体”，应随镜像构建：

- `src/yk_ferta/`
- `api/` 中仍被当前主流程复用的共享模块
- `tools/` 中仍被当前主流程复用的共享模块
- `hpo_extractor.py`
- `config/` 中不含密钥的默认模板
- `pyproject.toml`
- 启动脚本与容器入口脚本
- `src/yk_ferta/api/static/` 前端静态页

说明：

- 当前运行链路仍显式依赖 `api.interface`、`tools.*`、`hpo_extractor.py`，因此不能只打包 `src/yk_ferta`。
- 这部分后续可以继续内聚，但第一版容器化不应先做大规模重构。

### 2.2 不应写入镜像的内容

这些内容不应作为镜像固定内容：

- `database/` 全量数据
- `data/yk_ferta.sqlite3`
- HuggingFace 缓存目录
- API key / base URL / token
- 环境特定的配置文件
- 日志输出
- 临时结果与 artifact 导出目录

原因：

- `database/` 当前约 `2.0G`，且会持续演化，不适合每次跟随镜像重打。
- SQLite、日志、缓存都属于运行态数据，应持久化挂载。
- 密钥和外部服务地址不能固化进镜像。

## 3. 配置分层建议

容器化时应把配置拆成三层，而不是继续依赖单个带密钥的 `clinical_mvp.json`。

### 3.1 镜像内默认配置模板

镜像内保留一份默认模板，例如：

- `config/clinical_mvp.template.json`

用途：

- 提供默认结构
- 说明有哪些配置项
- 可作为挂载配置的参考模板

要求：

- 不包含真实 API key
- 不包含环境专属地址
- 不包含服务器本地绝对路径

### 3.2 挂载配置文件

部署时挂载真实配置文件，例如：

- `/app/config/clinical_mvp.json`

这份文件适合承载：

- 工作流参数
- 外部服务地址
- 检索数量
- 模型名
- 业务阈值

不适合承载：

- 长期密钥明文

### 3.3 环境变量

环境变量只承载部署环境敏感项和运行时路径：

建议包括：

- `YK_FERTA_CONFIG_PATH`
- `YK_FERTA_DB_PATH`
- `OPENAI_API_KEY`
- `OPENAI_BASE_URL`
- `HF_HOME`
- `TRANSFORMERS_CACHE`
- `PORT`
- `HOST`

说明：

- 当前代码还未完全以环境变量驱动全部配置项，但容器化设计应先按这个方向收口。
- 第一版可接受“环境变量 + 挂载配置文件”并存。

## 4. 数据与挂载目录设计

推荐容器内路径如下：

- `/app`：程序目录
- `/app/config`：挂载配置
- `/app/database`：挂载业务数据
- `/app/runtime`：挂载 SQLite、日志、运行态文件
- `/app/.cache/huggingface`：挂载模型缓存

推荐挂载项：

1. `database/ -> /app/database`
2. `runtime/ -> /app/runtime`
3. `config/clinical_mvp.json -> /app/config/clinical_mvp.json`
4. `hf-cache/ -> /app/.cache/huggingface`

其中：

- SQLite 建议放在 `/app/runtime/yk_ferta.sqlite3`
- 不建议写回仓库内的 `data/`

## 5. 对外服务与容器职责

### 5.1 yk-FERTA 核心容器职责

`yk-ferta-core` 容器对外只提供：

- `GET /healthz`
- `GET /docs`
- `GET /openapi.json`
- `POST /api/v1/cases`
- `POST /api/v1/tasks`
- `GET /api/v1/tasks/{task_id}/events`
- `GET /api/v1/tasks/{task_id}/result`
- 以及现有静态演示页

### 5.2 不建议打包进同一镜像的内容

以下内容建议保持容器外部依赖，不要和主服务揉成一个大镜像：

- `RAG-HPO` 服务
- 外部 LLM API
- HPO 官方 API
- PubMed / Web 检索入口
- 可选 phenotype 工具服务

理由：

- 这些本身就是独立依赖
- 生命周期、升级频率、网络策略不同
- 打包进同一镜像会让职责混乱，调试困难

## 6. 当前代码对容器化的实际影响

基于当前代码，容器化时需要特别注意以下几点。

### 6.1 当前代码依赖相对路径

例如：

- `./database/...`
- `config/clinical_mvp.json`
- `data/yk_ferta.sqlite3`

这意味着容器运行时必须保证：

- 工作目录稳定
- 挂载路径与配置路径一致

因此第一版容器建议：

- `WORKDIR /app`
- 所有相对路径在容器内都以 `/app` 为基准

### 6.2 当前配置文件中混有敏感信息

当前 `config/clinical_mvp.json` 中已有 API key 和环境依赖地址。

容器化前应明确：

- 镜像中不放真实密钥
- 真实配置文件由部署时挂载
- 后续再逐步把密钥迁到环境变量

### 6.3 当前疾病标准化依赖 HuggingFace 模型缓存

虽然 HuggingFace 在中国大陆不一定直连可用，但这不影响容器化设计。

正确做法是：

- 允许模型缓存目录外挂
- 允许离线准备缓存后挂载到容器内
- 不把这部分缓存硬塞进镜像

### 6.4 当前检索和病例库数据量不适合内嵌镜像

`database/` 当前约 `2.0G`，后续还会继续变。

因此：

- 不建议把 `database/` 打进镜像
- 建议作为明确的数据卷挂载

## 7. 第一版 Docker 服务形态建议

### 7.1 进程模型

第一版最简单进程模型：

- 单进程 `uvicorn`

例如：

```bash
uvicorn yk_ferta.api.app:app --host 0.0.0.0 --port 8000
```

说明：

- 这一步的目标是跑通和交付，不是高并发优化
- 等实际部署链路稳定后，再考虑 gunicorn/uvicorn worker 模型

### 7.2 容器启动所需最小输入

必须提供：

1. 挂载的 `database/`
2. 挂载的真实配置文件
3. 挂载的运行目录
4. 必要环境变量

如果缺少这些，容器不应被视为可运行交付物。

## 8. 推荐的镜像/挂载职责表

### 8.1 写入镜像

- Python 运行环境
- 应用代码
- 静态前端页面
- 默认配置模板
- 启动脚本

### 8.2 作为挂载

- `database/`
- SQLite 文件目录
- HuggingFace 缓存目录
- 真实配置文件
- 日志目录

### 8.3 作为环境变量

- API keys
- 外部服务基地址
- 缓存路径
- 启动端口和绑定地址

## 9. 第一版不做的事情

第一版容器化先不做这些：

- 把 `RAG-HPO` 合并进同一镜像
- 把所有数据都 baking 到镜像里
- 做复杂多容器编排
- 做生产级日志采集
- 做 K8s 适配
- 做鉴权和平台网关整合

这些都不属于当前“核心智能体服务容器化”的最小目标。

## 10. 下一步建议

容器化按这个顺序推进：

1. 先补容器化文档和边界
2. 再补：
   - `Dockerfile`
   - `.dockerignore`
   - 最小 `docker-compose.yml`
3. 本地先用挂载目录跑通
4. 再考虑把镜像传到目标服务器
5. 最后再处理 nginx 反代和公网入口

## 11. 当前建议结论

当前 yk-FERTA 的 Docker 交付应遵循以下原则：

- 镜像只装“程序本体”，不装大数据和运行态文件
- `database/`、SQLite、缓存、真实配置必须外挂
- `RAG-HPO` 保持独立服务，不并入主镜像
- 先做单容器可运行，再做服务器接入

