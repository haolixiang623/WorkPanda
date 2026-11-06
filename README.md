# WorkPanda

WorkPanda 是面向群众的自然语言公共服务入口。当前仓库处于**工程骨架**阶段：已建立统一业务后端、固定流程执行、任务状态与确认凭证机制，并具备受控执行能力（模型可调用受控工具、结果可核验）；尚未接入具体业务事项与外部业务系统。

工程实现细节见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)，受控执行层见 [docs/HARNESS.md](docs/HARNESS.md)，协作规范见 [AGENTS.md](AGENTS.md) 与 [docs/DEVELOPMENT-OPERATING-MECHANISM.md](docs/DEVELOPMENT-OPERATING-MECHANISM.md)，界面约束见 [design/DESIGN.md](design/DESIGN.md)。

## 已实现

- FastAPI 统一业务后端，客户端只访问宿主 API；
- 服务端固定流程定义与版本解析；
- 任务状态机，含澄清、等待确认、待确认结果和转人工；
- 槽位收集与多轮澄清；
- 关键动作的服务端一次性确认凭证（绑定任务、流程版本、步骤和参数摘要）；
- 签名会话令牌、任务归属隔离与按会话限流；
- **通用 harness 受控执行层**：工具白名单、参数校验、三重预算、逐次留痕；
- 结构化结果契约：没有可核验依据不得报成功，工具不可用不伪装完成；
- Octop-Harness 运行时适配器（备选后端，可选启用）；
- 运维只读接口 `/api/ops/*`，供 Octop 控制台读取；
- 执行器、意图路由、事件发布的可替换端口；
- SSE 事件流，支持断线后按序号补发；
- 内存与 SQLite 两种存储；
- 统一错误码与单元/接口测试。

## 未实现

- 任何真实公共服务事项与话术；
- 真实业务系统工具（尚无外部 API 接入）；
- Octop 控制台插件、Octop Browser；
- 登录、实名核验、权限分级与审计后台；
- 流程管理后台、发布审核；
- PostgreSQL、多实例部署、监控告警。

## 环境要求

- Python 3.12 或更高版本
- 依赖与工具配置在 `pyproject.toml`

## 快速开始

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
uvicorn workpanda.main:app --app-dir src --reload
```

启动后：

- 健康检查：`GET http://127.0.0.1:8000/healthz`
- 接口文档：`http://127.0.0.1:8000/docs`

## 接口

所有 `/api` 请求都需要携带 `X-WorkPanda-Session`。首次请求可以不带，服务端会在响应头下发新会话，客户端后续请求需原样带回。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 健康检查 |
| POST | `/api/tasks` | 创建任务并推进到第一个阻塞点 |
| GET | `/api/tasks/{task_id}` | 查询任务 |
| POST | `/api/tasks/{task_id}/answers` | 补充字段并继续 |
| POST | `/api/tasks/{task_id}/confirm` | 提交确认意图 |
| POST | `/api/tasks/{task_id}/cancel` | 取消任务 |
| GET | `/api/tasks/{task_id}/events` | SSE 事件流，支持 `?after=` 补发 |

运维只读接口（需 `X-WorkPanda-Ops-Token`，未配置 `WORKPANDA_OPS_TOKEN` 时整体 404）：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/ops/tasks` | 列出任务，支持 `limit`、`status` |
| GET | `/api/ops/tasks/{task_id}` | 任务详情 |
| GET | `/api/ops/tasks/{task_id}/events` | 事件列表 |

任务按会话隔离：访问不属于自己的任务返回 404，不暴露任务是否存在。超限返回 429 并带 `Retry-After`。

## 配置

环境变量前缀 `WORKPANDA_`：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `WORKPANDA_STORE_BACKEND` | `memory` | `memory` 或 `sqlite` |
| `WORKPANDA_SQLITE_PATH` | `workpanda.db` | SQLite 文件路径 |
| `WORKPANDA_INTENT_CONFIDENCE_THRESHOLD` | `0.6` | 意图匹配最低置信度 |
| `WORKPANDA_CONFIRMATION_TTL_SECONDS` | `300` | 确认凭证有效期 |
| `WORKPANDA_EVENT_KEEPALIVE_SECONDS` | `15` | SSE 心跳间隔 |
| `WORKPANDA_SESSION_SECRET` | 进程随机值 | 会话令牌签名密钥；未配置时重启后旧会话失效 |
| `WORKPANDA_SESSION_TTL_SECONDS` | `43200` | 会话有效期 |
| `WORKPANDA_RATE_LIMIT_PER_MINUTE` | `120` | 每会话每分钟请求数 |
| `WORKPANDA_RATE_LIMIT_BURST` | `30` | 每会话瞬时突发上限 |
| `WORKPANDA_OPS_TOKEN` | 空 | 运维只读接口凭据；为空时接口不可用 |
| `WORKPANDA_HARNESS_ENABLED` | `false` | 启用通用 harness 执行器（优先于 Octop） |
| `WORKPANDA_MODEL_BASE_URL` | 空 | OpenAI 兼容接口地址 |
| `WORKPANDA_MODEL_API_KEY` | 空 | 模型凭据；启用 harness 时必填 |
| `WORKPANDA_MODEL_NAME` | `gpt-4o-mini` | 模型标识 |
| `WORKPANDA_MODEL_TIMEOUT_SECONDS` | `60` | 单次模型调用超时 |
| `WORKPANDA_MODEL_TRUST_ENV` | `true` | 是否沿用系统代理变量 |
| `WORKPANDA_HARNESS_MAX_STEPS` | `6` | 单步骤最多模型轮次 |
| `WORKPANDA_HARNESS_MAX_TOOL_CALLS` | `8` | 单步骤最多工具调用次数 |
| `WORKPANDA_HARNESS_DEADLINE_SECONDS` | `45` | 单步骤总耗时上限 |
| `WORKPANDA_HARNESS_ALLOWED_HOSTS` | 空 | 逗号分隔的 http 工具域名白名单；为空则不注册 |
| `WORKPANDA_OCTOP_ENABLED` | `false` | 启用 Octop-Harness 运行时执行器 |
| `WORKPANDA_OCTOP_API_KEY` | 空 | 模型凭据；启用时必填 |
| `WORKPANDA_OCTOP_MODEL` | `gpt-4o-mini` | 模型标识 |
| `WORKPANDA_OCTOP_PROVIDER` | `openai` | `openai` 或 `anthropic` |
| `WORKPANDA_OCTOP_BASE_URL` | 空 | 兼容接口地址 |
| `WORKPANDA_OCTOP_WORKSPACE` | `.octop/workpanda` | Agent 工作区，必须可解析为绝对路径 |
| `WORKPANDA_OCTOP_TIMEOUT_SECONDS` | `30` | 单步骤执行超时 |
| `WORKPANDA_ENABLE_DEMO_FLOW` | `true` | 启用骨架自检流程 `demo.generic` |

Octop 集成细节见 [docs/OCTOP-INTEGRATION.md](docs/OCTOP-INTEGRATION.md)。启用运行时时需安装可选依赖：

```bash
python -m pip install -e '.[octop]'
```

`demo.generic` 只用于验证"收集 → 确认 → 执行 → 完成"链路，不代表任何业务规则。正式接入业务前必须设为 `false`。

## 验证

```bash
pytest
ruff check .
```

## 目录结构

```text
src/workpanda/
├── api/             路由、错误映射、依赖
├── application/     任务服务、流程推进器、端口协议
├── domain/          状态机、流程定义、确认凭证、存储协议
├── harness/         通用受控执行层（工具循环、预算、留痕、契约）
├── infrastructure/  内存/SQLite 存储、事件总线、Octop 适配器
├── bootstrap.py     依赖装配
├── config.py        配置
└── main.py          应用入口

tests/               单元与接口测试
scripts/             端到端冒烟脚本
docs/                架构、受控执行层与开发运营机制
design/              UI/UX 设计契约
```

## 架构边界

- 业务状态以 Task Store 为权威，Runtime checkpoint 未来只用于恢复 Agent 会话；
- 客户端不能指定工具、推进步骤或跳过确认；
- 关键动作的确认由服务端凭证强制，而非提示词或按钮样式；
- 外部结果未知时进入"待确认"，不自动重试。
