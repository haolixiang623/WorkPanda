# WorkPanda

WorkPanda 是面向群众的自然语言公共服务入口。当前仓库处于工程骨架阶段，先建立统一业务后端、任务状态和固定流程执行边界，暂不接入具体业务事项或外部执行器。

## 当前范围

已提供：

- Python 3.12+ 与 FastAPI 应用入口；
- 通用任务模型、状态和事件模型；
- 可替换的 Task Store 抽象及开发期内存实现；
- 任务创建、查询、取消和 SSE 事件接口；
- 健康检查和基础测试；
- 项目级 AI coding、UI 设计和开发运营规范。

暂不包含：

- 具体公共服务流程；
- 真实业务 API、Octop-Harness、浏览器自动化；
- 认证、生产数据库和生产部署；
- 任何真实用户数据。

## 环境要求

- Python 3.12 或更高版本；
- 推荐使用虚拟环境；
- 依赖和开发工具定义在 `pyproject.toml`。

## 快速开始

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
uvicorn workpanda.main:app --app-dir src --reload
```

服务启动后：

- 健康检查：`GET http://127.0.0.1:8000/healthz`
- OpenAPI：`http://127.0.0.1:8000/docs`
- 创建通用任务：`POST /api/tasks`
- 查询任务：`GET /api/tasks/{task_id}`
- 取消任务：`POST /api/tasks/{task_id}/cancel`
- 事件流：`GET /api/tasks/{task_id}/events`

## 验证

```bash
pytest
ruff check .
```

当前内存 Task Store 只适合本地开发和测试，进程重启后数据会丢失。接入具体业务前，需要实现持久化 Task Store、鉴权、审计和运行时配置。

## 目录结构

```text
src/workpanda/
├── api/             HTTP 路由和依赖
├── application/     任务应用服务
├── domain/          任务、状态、事件和存储抽象
├── infrastructure/  内存存储等基础设施适配器
└── main.py          FastAPI 应用入口

tests/               单元与 API 测试
docs/                开发运营机制
design/              UI/UX 设计契约
```

## 架构边界

客户端只访问宿主业务 API，不直接连接模型 Runtime。业务状态以 Task Store 为权威；Runtime checkpoint 未来只用于恢复 Agent 运行，不替代业务任务状态。所有具体流程、工具白名单、确认凭证和外部执行器应在后续设计评审后接入。
