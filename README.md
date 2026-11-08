# WorkPanda

WorkPanda 是面向群众的自然语言公共服务入口。当前仓库处于**工程骨架**阶段：已建立统一业务后端、固定流程执行、任务状态与确认凭证机制，并具备受控执行能力（模型可调用受控工具、结果可核验）；尚未接入具体业务事项与外部业务系统。

业务目标见 [business-goals.md](business-goals.md)，目标架构见 [recommended-architecture.md](recommended-architecture.md)。工程实现细节见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)，受控执行层见 [docs/HARNESS.md](docs/HARNESS.md)，识别与展示契约（含评测集）见 [docs/INTENT-EVAL.md](docs/INTENT-EVAL.md)，发布前回放与发布门禁见 [docs/SOP-REPLAY.md](docs/SOP-REPLAY.md)，管理侧配置台见 [docs/ADMIN-CONSOLE.md](docs/ADMIN-CONSOLE.md)，容量基线与扩容 runbook 见 [docs/CAPACITY-RUNBOOK.md](docs/CAPACITY-RUNBOOK.md)，协作规范见 [AGENTS.md](AGENTS.md) 与 [docs/DEVELOPMENT-OPERATING-MECHANISM.md](docs/DEVELOPMENT-OPERATING-MECHANISM.md)，界面约束见 [design/DESIGN.md](design/DESIGN.md)。

## 已实现

- FastAPI 统一业务后端，客户端只访问宿主 API；
- 服务端固定流程定义与版本解析；
- 任务状态机，含澄清、等待确认、待确认结果和转人工；
- 槽位收集与多轮澄清；
- 确定性意图识别与槽位抽取：同分跨流程判歧义→澄清、抽不到不编造、
  未声明字段在创建入口即拒绝（版本化评测集见 [docs/INTENT-EVAL.md](docs/INTENT-EVAL.md)）；
- 群众可见状态（`ui_state`）由服务端唯一给出，只有"已完成"使用成功语义；
- 关键动作的服务端一次性确认凭证（绑定任务、流程版本、步骤和参数摘要）；
- **流程定义持久化**：版本不可变、发布状态由指针表达；发布/停用/回滚 = 移动指针，
  历史版本保留，回滚后在途任务仍按创建时的版本继续（Issue #49）；
- **发布前回放**：按 `(flow_id, version)` 用固定用例跑一遍，断言步骤序列、工具集合、
  终态与"不得出现未授权工具"；回放用不持有工具注册表的隔离执行器，带副作用的工具
  一律按未确认记账。用例与版本一起存库，有用例时回放未通过不得发布
  （见 [docs/SOP-REPLAY.md](docs/SOP-REPLAY.md)，Issue #50）；
- **管理侧 SOP 配置台**（Issue #51）：独立凭据与 viewer/admin 角色、表单化草稿编辑、
  回放用例维护、发布预演 / 发布 / 停用 / 回滚；发布后进程内流程目录原地重载，
  群众侧下一次创建任务即命中新版本，在途任务仍按各自版本推进
  （见 [docs/ADMIN-CONSOLE.md](docs/ADMIN-CONSOLE.md)）；
- 签名会话令牌、任务归属隔离与按会话限流；
- **通用 harness 受控执行层**：工具白名单、参数校验、四重预算（步数/调用/token/耗时）、
  工具限期执行与有界重试、非幂等操作防重复提交、逐次留痕；
- 结构化结果契约：没有可核验依据不得报成功，工具不可用不伪装完成；
- 落盘脱敏：敏感入参、手机号/身份证/银行卡/令牌在留痕与运维接口中统一掩码；
- 执行留痕独立存储（内存或 SQLite，可跨重启检索）与聚合指标（未确认占比、被拦/超时调用数）；
- 断点续跑（可选）：工具调用后落 checkpoint，中断可继续；出现结果未知时拒绝续跑并转人工；
- 运维只读接口 `/api/ops/*`，供 Octop 控制台读取；
- 执行器、意图路由、事件发布的可替换端口；
- SSE 事件流，支持断线后按序号补发；
- 内存与 SQLite 两种存储；
- 统一错误码与单元/接口测试。

## 未实现

- 任何真实公共服务事项与话术（意图规则目前只挂在自检流程上）；
- 真实业务系统工具（尚无外部 API 接入）；
- Octop 控制台插件、Octop Browser；
- 登录、实名核验、权限分级与审计后台；
- 多级审批流、意图样例的运行时路由（Issue #52）与多实例发布同步（Issue #53）；
  管理侧配置台已可用（Issue #51），但启用前仍需业务与安全责任人确认权限边界，
  且真实浏览器里的一次完整操作走查仍待人工完成；
- PostgreSQL 生产存储（Issue #34）、完整的多实例部署（限流与事件总线仍是单进程语义）、
  监控告警平台。容量指标端点 `/api/ops/metrics`、实例数声明与多实例启动一致性门禁
  已可用，但阈值待业务/安全责任人确认、双实例真实冒烟未跑，详见
  [docs/CAPACITY-RUNBOOK.md](docs/CAPACITY-RUNBOOK.md)。

## 环境要求

- Python 3.12 或更高版本
- 依赖与工具配置在 `pyproject.toml` 和 `requirements-dev.txt`（开发工具固定版本）

## 快速开始

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m pip install -r requirements-dev.txt
uvicorn workpanda.main:app --app-dir src --reload
```

启动后：

- 健康检查：`GET http://127.0.0.1:8000/healthz`
- 接口文档：`http://127.0.0.1:8000/docs`

## 本地模拟三方站

`mock-site/` 是首期试点的模拟第三方站点：独立应用、两个端口、两个 origin、三个 tab、
多附件，并保留五个可配置的失败/未知开关。它是 P2 浏览器执行器与 P3 人机协同的靶子。
**这是模拟站，任何行为都不代表真实政务系统**，也不接真实身份核验与实名通道。

```bash
cd mock-site && python -m mock_site
```

演示账号 `demo / demo1234`，账号站 `127.0.0.1:8101`、办理站 `127.0.0.1:8102`。
细节与全部开关见 [mock-site/README.md](mock-site/README.md)。

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

运维只读接口（需 `X-WorkPanda-Ops-Token`，未配置 `WORKPANDA_OPS_TOKEN` / `WORKPANDA_OPS_TOKENS`
时整体 404）。`viewer` 角色只能读任务列表与留痕聚合，其余明细端点要求 `admin`
（角色说明与轮换步骤见 [docs/OPS-ACCESS-CONTROL.md](docs/OPS-ACCESS-CONTROL.md)）：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/ops/tasks` | 列出任务，支持 `limit`、`status`、`flow_id`、`since`/`until`、`cursor` |
| GET | `/api/ops/tasks/{task_id}` | 任务详情 |
| GET | `/api/ops/tasks/{task_id}/events` | 事件列表，支持 `after`、`type`、`since`/`until`、`limit` |
| GET | `/api/ops/traces` | 执行留痕，支持 `task_id`、`limit`、`outcome`/`stop_reason`/`risk`、`cursor` |
| GET | `/api/ops/traces/summary` | 执行指标聚合，支持 `task_id` |
| GET | `/api/ops/tool-runtime` | 工具线程台账与容量闸门（要求 `admin`） |
| GET | `/api/ops/metrics` | 容量基线指标与部署形态（要求 `admin`）；不可测项为 `value=null` + 原因，不返回 0 |
| GET | `/api/ops/audit` | 审计归档，支持 `task_id`、`kind`、`since`/`until`、`cursor` |

列表接口的响应体是数组，翻页靠 `X-Next-Cursor` 响应头（没有这个头就是到头了）。

`/api/ops/metrics` 返回逐项指标状态（`ok` / `not_applicable` / `not_instrumented` /
`error`）、`degraded` 与埋点缺口清单；阈值与「超过后必须做什么」不写在接口里，
见 [docs/CAPACITY-RUNBOOK.md](docs/CAPACITY-RUNBOOK.md)（阈值属待确认的工程初值）。

管理侧配置台接口 `/api/admin/flows/*` 使用第三套独立凭据
`X-WorkPanda-Admin-Token`（`WORKPANDA_ADMIN_TOKENS`），未配置时整体 404；
`viewer` 只读，`admin` 才能写版本、用例与发布指针。发布先过回放门禁，
`apply=false` 可预演而不移动指针。接口清单、权限矩阵、影响面与已知限制见
[docs/ADMIN-CONSOLE.md](docs/ADMIN-CONSOLE.md)，前端在 `admin-ui/`。

任务按会话隔离：访问不属于自己的任务返回 404，不暴露任务是否存在。超限返回 429 并带 `Retry-After`。

任务响应带 `ui_state`（群众可见状态键，与 `status` 一一对应，映射见
[docs/INTENT-EVAL.md](docs/INTENT-EVAL.md) 第 3.3 节）；`clarification_required`
错误体同样带 `ui_state: needs_clarification`。

## 配置

环境变量前缀 `WORKPANDA_`：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `WORKPANDA_STORE_BACKEND` | `memory` | `memory` 或 `sqlite` |
| `WORKPANDA_SQLITE_PATH` | `workpanda.db` | SQLite 文件路径 |
| `WORKPANDA_EXPECTED_INSTANCES` | `1` | 部署方声明的实例数（非探测值）；`>= 2` 时启动强制要求 `redis` 共享后端、步骤租约与非 `memory` 任务存储，见 [docs/CAPACITY-RUNBOOK.md](docs/CAPACITY-RUNBOOK.md) |
| `WORKPANDA_INTENT_CONFIDENCE_THRESHOLD` | `0.6` | 意图匹配最低置信度 |
| `WORKPANDA_CONFIRMATION_TTL_SECONDS` | `300` | 确认凭证有效期 |
| `WORKPANDA_EVENT_KEEPALIVE_SECONDS` | `15` | SSE 心跳间隔 |
| `WORKPANDA_SESSION_SECRET` | 进程随机值 | 会话令牌签名密钥；未配置时重启后旧会话失效 |
| `WORKPANDA_SESSION_TTL_SECONDS` | `43200` | 会话有效期 |
| `WORKPANDA_RATE_LIMIT_PER_MINUTE` | `120` | 每会话每分钟请求数 |
| `WORKPANDA_RATE_LIMIT_BURST` | `30` | 每会话瞬时突发上限 |
| `WORKPANDA_OPS_TOKEN` | 空 | 单主体运维凭据（等价 admin）；为空且未配 `OPS_TOKENS` 时接口不可用 |
| `WORKPANDA_OPS_TOKENS` | 空 | 多主体运维凭据 `主体:角色:密钥`，逗号分隔，角色取 `admin`/`viewer` |
| `WORKPANDA_ADMIN_TOKENS` | 空 | 管理侧配置台凭据，格式同上但独立发放；为空时 `/api/admin/flows/*` 整体不可用 |
| `WORKPANDA_BIND_HOST` | `127.0.0.1` | 监听地址；非回环需显式确认并启用 TLS |
| `WORKPANDA_BIND_PORT` | `8000` | 监听端口 |
| `WORKPANDA_ALLOW_NETWORK_BIND` | `false` | 确认把服务暴露到非回环地址（安全边界变更） |
| `WORKPANDA_TLS_ENABLED` | `false` | 启用 TLS；开启时必须提供证书与私钥 |
| `WORKPANDA_TLS_CERTFILE` | 空 | 证书链路径 |
| `WORKPANDA_TLS_KEYFILE` | 空 | 私钥路径 |
| `WORKPANDA_CORS_ORIGINS` | 空 | 跨域来源白名单，逗号分隔；不接受通配符 |
| `WORKPANDA_HARNESS_ENABLED` | `false` | 启用通用 harness 执行器（唯一的真实执行路径） |
| `WORKPANDA_MODEL_BASE_URL` | 空 | OpenAI 兼容接口地址 |
| `WORKPANDA_MODEL_API_KEY` | 空 | 模型凭据；启用 harness 时必填 |
| `WORKPANDA_MODEL_NAME` | `gpt-4o-mini` | 模型标识 |
| `WORKPANDA_MODEL_TIMEOUT_SECONDS` | `60` | 单次模型调用超时 |
| `WORKPANDA_MODEL_TRUST_ENV` | `true` | 是否沿用系统代理变量 |
| `WORKPANDA_HARNESS_MAX_STEPS` | `6` | 单步骤最多模型轮次 |
| `WORKPANDA_HARNESS_MAX_TOOL_CALLS` | `8` | 单步骤最多工具调用次数 |
| `WORKPANDA_HARNESS_MAX_TOKENS` | `0` | 单步骤 token 上限；0 不限，仅在上游上报 usage 时生效 |
| `WORKPANDA_HARNESS_DEADLINE_SECONDS` | `45` | 单步骤总耗时上限（兼作单次工具兜底超时） |
| `WORKPANDA_HARNESS_TOOL_RETRIES` | `1` | 可重试失败的额外尝试次数，仅对幂等工具生效 |
| `WORKPANDA_HARNESS_REDACT` | `true` | 落盘脱敏开关 |
| `WORKPANDA_HARNESS_TRACE_CAPACITY` | `500` | trace 保留条数上限；0 不淘汰 |
| `WORKPANDA_HARNESS_TRACE_BACKEND` | `auto` | `memory` / `sqlite` / `auto`（跟随 `STORE_BACKEND`） |
| `WORKPANDA_HARNESS_RESUME_ENABLED` | `false` | 断点续跑；开启后每个工具调用落 checkpoint |
| `WORKPANDA_HARNESS_RESUME_TTL_SECONDS` | `900` | 断点保留时限，超时拒绝续跑 |
| `WORKPANDA_HARNESS_ALLOWED_HOSTS` | 空 | 逗号分隔的 http 工具域名白名单；为空则不注册 |
| `WORKPANDA_ENABLE_DEMO_FLOW` | `true` | 启用骨架自检流程 `demo.generic` |
| `WORKPANDA_REPLAY_RELEASE_GATE` | `true` | 该版本有回放用例时，回放未通过不得发布 |
| `WORKPANDA_REPLAY_TIMEOUT_SECONDS` | `10` | 单条回放用例的时限；超时判"无结论" |

运维访问控制（角色、凭据轮换、TLS、回退）见
[docs/OPS-ACCESS-CONTROL.md](docs/OPS-ACCESS-CONTROL.md)。

`demo.generic` 只用于验证"收集 → 确认 → 执行 → 完成"链路，不代表任何业务规则。正式接入业务前必须设为 `false`。

默认只监听 `127.0.0.1` 且不启用 TLS。绑定非回环地址会因缺少确认或 TLS 直接启动失败，
这是有意的：把政务服务明文挂到网络上不会有任何报错。

## 验证

```bash
pytest
ruff check .
```

## 目录结构

```text
src/workpanda/
├── api/             路由、错误映射、依赖
├── application/     任务服务、流程推进器、回放与发布门禁、端口协议
├── domain/          状态机、流程定义、回放用例与断言、确认凭证、存储协议
├── harness/         通用受控执行层（工具循环、预算、脱敏、留痕、契约）
├── infrastructure/  内存/SQLite 存储、事件总线
├── bootstrap.py     依赖装配
├── config.py        配置
└── main.py          应用入口

tests/               单元与接口测试
scripts/             端到端冒烟、压测与回放脚本
mock-site/           本地模拟三方站（两个 origin、多 tab、多附件、可复现失败）
docs/                架构、受控执行层与开发运营机制
design/              UI/UX 设计契约
```

## 架构边界

- 业务状态以 Task Store 为权威，Runtime checkpoint 未来只用于恢复 Agent 会话；
- 客户端不能指定工具、推进步骤或跳过确认；
- 关键动作的确认由服务端凭证强制，而非提示词或按钮样式；
- 外部结果未知时进入"待确认"，不自动重试。
