# WorkPanda

WorkPanda 是面向群众的自然语言公共服务入口。当前仓库处于**工程骨架**阶段：已建立统一业务后端、固定流程执行、任务状态与确认凭证机制，尚未接入具体业务事项、真实模型和外部执行器。

工程实现细节见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)，协作规范见 [AGENTS.md](AGENTS.md) 与 [docs/DEVELOPMENT-OPERATING-MECHANISM.md](docs/DEVELOPMENT-OPERATING-MECHANISM.md)，界面约束见 [design/DESIGN.md](design/DESIGN.md)。

## 已实现

- FastAPI 统一业务后端，客户端只访问宿主 API；
- 服务端固定流程定义与版本解析；
- 任务状态机，含澄清、等待确认、待确认结果和转人工；
- 槽位收集与多轮澄清；
- 关键动作的服务端一次性确认凭证（绑定任务、流程版本、步骤和参数摘要）；
- 执行器、意图路由、事件发布的可替换端口；
- SSE 事件流，支持断线后按序号补发；
- 内存与 SQLite 两种存储；
- 统一错误码与单元/接口测试。

## 未实现

- 任何真实公共服务事项与话术；
- Octop-Harness、Octop Browser、真实外部 API；
- 用户认证、实名核验、限流与审计后台；
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

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 健康检查 |
| POST | `/api/tasks` | 创建任务并推进到第一个阻塞点 |
| GET | `/api/tasks/{task_id}` | 查询任务 |
| POST | `/api/tasks/{task_id}/answers` | 补充字段并继续 |
| POST | `/api/tasks/{task_id}/confirm` | 提交确认意图 |
| POST | `/api/tasks/{task_id}/cancel` | 取消任务 |
| GET | `/api/tasks/{task_id}/events` | SSE 事件流，支持 `?after=` 补发 |

## 配置

环境变量前缀 `WORKPANDA_`：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `WORKPANDA_STORE_BACKEND` | `memory` | `memory` 或 `sqlite` |
| `WORKPANDA_SQLITE_PATH` | `workpanda.db` | SQLite 文件路径 |
| `WORKPANDA_INTENT_CONFIDENCE_THRESHOLD` | `0.6` | 意图匹配最低置信度 |
| `WORKPANDA_CONFIRMATION_TTL_SECONDS` | `300` | 确认凭证有效期 |
| `WORKPANDA_EVENT_KEEPALIVE_SECONDS` | `15` | SSE 心跳间隔 |
| `WORKPANDA_ENABLE_DEMO_FLOW` | `true` | 启用骨架自检流程 `demo.generic` |

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
├── infrastructure/  内存/SQLite 存储、事件总线、开发期适配器
├── bootstrap.py     依赖装配
├── config.py        配置
└── main.py          应用入口

tests/               单元与接口测试
docs/                架构与开发运营机制
design/              UI/UX 设计契约
```

## 架构边界

- 业务状态以 Task Store 为权威，Runtime checkpoint 未来只用于恢复 Agent 会话；
- 客户端不能指定工具、推进步骤或跳过确认；
- 关键动作的确认由服务端凭证强制，而非提示词或按钮样式；
- 外部结果未知时进入"待确认"，不自动重试。
