# WorkPanda 工程骨架架构

> 本文件描述**已实现**的骨架结构，不代表最终业务架构。业务目标与推荐架构仍以父目录的 `business-goals.md` 和 `recommended-architecture.md` 为准；两者进入版本控制前，本文件只记录工程实现事实。

## 1. 骨架边界

已实现：

- 统一业务后端（FastAPI）作为群众端唯一入口；
- 服务端固定流程定义与版本；
- 任务状态机与事件流（SSE）；
- 槽位收集与澄清暂停；
- 关键动作的服务端一次性确认凭证；
- 执行器、意图路由、事件发布的可替换端口；
- 内存与 SQLite 两种 Task Store 实现；
- 统一错误码映射与基础测试。

明确未实现：

- 具体公共服务事项、真实业务话术；
- Octop-Harness、Octop Browser、真实外部 API；
- 用户认证、实名核验、限流、审计后台；
- 流程管理后台与发布审核界面；
- PostgreSQL 生产存储、多实例部署、监控告警；
- 模型调用与真实意图分类。

## 2. 分层与依赖方向

```text
api/             HTTP 接口与错误映射
  ↓
application/     应用服务、流程推进器、端口协议
  ↓
domain/          任务状态、流程定义、确认凭证、存储协议
  ↑
infrastructure/  存储、事件总线、开发期适配器
```

依赖方向单向：`api → application → domain`，`infrastructure` 只实现 `domain` 和 `application` 定义的协议。`domain` 不依赖任何框架。

## 3. 关键组件

### 3.1 固定流程（`domain/flow.py`）

流程由服务端注册，包含有序步骤。步骤类型只有三种：

| 类型 | 作用 |
|---|---|
| `collect` | 收集必填字段；缺失时暂停并要求澄清 |
| `execute` | 调用白名单内的工具；可选要求群众确认 |
| `handoff` | 结束自动化，转人工 |

流程校验保证：步骤 ID 唯一；`execute` 必须声明工具；非执行步骤不得声明工具或要求确认。流程注册表支持按版本解析，未发布版本不可运行。

### 3.2 状态机（`domain/state_machine.py`）

```text
created → running → waiting_for_user → running
                  → waiting_for_confirmation → running
                  → completed / failed / handoff_required / outcome_unconfirmed
                  → cancelled
```

约束：

- 终态不可回到运行态；
- `outcome_unconfirmed`（外部结果未知）只能转人工，不能直接标记完成或失败；
- 未列出的转换一律拒绝，避免掩盖真实结果。

### 3.3 流程推进器（`application/runner.py`）

职责单一：按流程定义逐步推进并产生事件。

- `collect`：缺字段则暂停，事件只包含字段名与提示，不写入敏感值；
- `execute`：若要求确认，先校验凭证；有效则一次性消费再执行；
- 执行结果分三类：成功、失败、结果未知；
- 失败默认转人工（`fail_to_handoff`），结果未知直接进入待确认，不重试。

### 3.4 确认凭证（`domain/confirmation.py`）

凭证绑定：任务、流程版本、步骤、参数摘要、用户引用、有效期。

- 群众确认时由服务端签发；
- 执行前重新计算参数摘要并校验；
- 参数在确认后变化、凭证过期或已被使用，都会重新要求确认；
- 凭证一次性消费，重复执行被拒绝。

这使"必须确认"成为服务端强制约束，而不是提示词或按钮样式。

### 3.5 事件流

- 事件持久化在 Task Store，顺序号由存储原子分配；
- `EventBroker` 只做在线即时通知，不承担可靠性；
- 客户端断线重连后按 `after` 序号补发，避免丢事件或重复。

### 3.6 存储

| 实现 | 用途 | 说明 |
|---|---|---|
| `InMemoryTaskStore` | 测试、本地试跑 | 进程重启丢失 |
| `SqliteStore` | 本地开发、单机试点 | 持久化任务、事件和凭证 |

两者实现同一组协议，业务层不感知差异。生产目标为 PostgreSQL，需补充并发控制、备份与审计。

## 4. 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 健康检查 |
| POST | `/api/tasks` | 创建任务，解析流程并推进到第一个阻塞点 |
| GET | `/api/tasks/{task_id}` | 查询任务状态 |
| POST | `/api/tasks/{task_id}/answers` | 补充字段，随后继续推进 |
| POST | `/api/tasks/{task_id}/confirm` | 提交确认意图；服务端签发凭证并继续 |
| POST | `/api/tasks/{task_id}/cancel` | 取消未终结任务 |
| GET | `/api/tasks/{task_id}/events` | SSE 事件流，支持 `after` 断线补发 |

客户端不直接指定工具、步骤或流程推进方式。

## 5. 错误码

| HTTP | code | 含义 |
|---|---|---|
| 404 | `task_not_found` | 任务不存在 |
| 404 | `flow_not_found` | 流程未注册 |
| 409 | `task_state_conflict` | 当前状态不允许该操作 |
| 409 | `invalid_transition` | 非法状态转换 |
| 409 | `confirmation_invalid` | 确认凭证不可用 |
| 422 | `request_rejected` | 参数或流程不满足约束 |
| 422 | `clarification_required` | 无法确定事项，需澄清或转人工 |
| 422 | `flow_not_published` | 流程存在但未发布 |

## 6. 已知限制

- SQLite 调用是同步的，直接运行在事件循环中；高并发场景需改为线程池或异步驱动；
- 事件总线是单进程内实现，多实例部署需要外部消息通道；
- 无认证与限流，`user_ref` 只是占位，不能作为身份依据；
- 无流程管理后台，流程目前通过代码注册；
- 无真实模型，意图路由是关键词占位实现（默认无规则）；
- 无审计日志与指标导出。

## 7. 下一步建议顺序

1. 接入真实身份与会话隔离；
2. 把 SQLite 替换为 PostgreSQL，并引入迁移工具；
3. 增加流程注册与发布审核的持久化配置；
4. 接入真实意图分类与槽位抽取，并建立版本化评测集；
5. 实现第一个低风险执行器（公开 API 优先），再接浏览器适配器；
6. 增加审计、指标和告警，再考虑灰度发布。
