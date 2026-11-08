# WorkPanda 工程骨架架构

> 本文件描述**已实现**的骨架结构，不代表最终业务架构。业务目标以 [business-goals.md](../business-goals.md) 为准，目标架构以 [recommended-architecture.md](../recommended-architecture.md) 为准；两者与本文件冲突时，业务与架构意图以后者为准，实现事实以本文件为准。

## 1. 骨架边界

已实现：

- 统一业务后端（FastAPI）作为群众端唯一入口；
- 服务端固定流程定义与版本；
- 任务状态机与事件流（SSE）；
- 槽位收集与澄清暂停；
- 关键动作的服务端一次性确认凭证；
- 执行器、意图路由、事件发布的可替换端口；
- 内存与 SQLite 两种 Task Store 实现；
- 签名会话令牌、任务归属隔离与按会话限流；
- 通用 harness 受控执行层：工具白名单、参数校验、三重预算、逐次留痕（见 `docs/HARNESS.md`）；
- 结构化结果契约（防虚假成功）：无依据不得报成功，有工具却零调用降级为待确认；
- 统一错误码映射与基础测试。

明确未实现：

- 具体公共服务事项、真实业务话术；
- Octop 控制台插件、Octop Browser、真实外部业务 API；
- 登录、实名核验、权限分级与审计后台；
- 流程管理后台与发布审核界面；
- PostgreSQL 生产存储、多实例部署、监控告警；
- 真实业务事项语义分类（当前是确定性规则，只挂在自检流程上；见 `docs/INTENT-EVAL.md`）。

## 2. 分层与依赖方向

```text
api/             HTTP 接口与错误映射
  ↓
application/     应用服务、流程推进器、端口协议
  ↓
domain/          任务状态、流程定义、确认凭证、存储协议
  ↑
harness/         通用受控执行层：工具循环、预算、留痕（只依赖 application.ports）
  ↑
infrastructure/  存储、事件总线
```

依赖方向单向：`api → application → domain`，`infrastructure` 与 `harness` 只实现 `domain` 和
`application` 定义的协议。`domain` 不依赖任何框架。`harness` 不认识业务流程，也不绑定模型厂商。

## 3. 关键组件

### 3.1 固定流程（`domain/flow.py`）

流程由服务端注册，包含有序步骤。步骤类型只有三种：

| 类型 | 作用 |
|---|---|
| `collect` | 收集必填字段；缺失时暂停并要求澄清 |
| `execute` | 调用白名单内的工具；可选要求群众确认 |
| `handoff` | 结束自动化，转人工 |

流程校验保证：步骤 ID 唯一；`execute` 必须声明工具；非执行步骤不得声明工具或要求确认。

**版本与发布**（`FlowStore`，实现见 `infrastructure/sqlite_flow_store.py` 与 `memory_store.py`）：

- 版本内容**不可变**：同一 `(flow_id, version)` 不允许覆盖写入，冲突直接报错；
- 发布状态由**指针**表达（`flow_releases`），发布 = 移动指针，回滚 = 把指针移回去，
  停用 = 标记指针已停用（`disabled_by` 非空，该行不再算发布指针）；历史版本一行都不删；
- 指针的每一次移动都带**署名**（`published_by` / `disabled_by`，见 `FlowRelease`）：
  `publish` / `disable` 的 `actor` 是仅关键字必填参数，值只能来自鉴权主体，不接受请求体
  自报——本系统不做双人复核，署名是发布链路上唯一的责任锚点；
- 装载时**派生**状态：指针指向的版本是 `PUBLISHED`，其余是 `DRAFT`。版本内容里的
  `status` 字段只是作者态标记，不参与运行语义——否则"发布"要改两条记录，改一半就是两种真相。

注册表有两种查询，分工是刻意的：

| 方法 | 语义 | 用在哪 |
|---|---|---|
| `resolve(flow_id, version=None)` | 只认发布指针；指定版本时不回退到"最大版本" | 新建任务的准入 |
| `require(flow_id, version)` | 只问到不到，不看发布状态 | 已建任务的推进、回答与确认 |

`require` 不看发布状态是因为回滚只移动指针：若它也要求"当前已发布"，一次回滚就会让
在途任务的回答与确认全部失败。

**发布前回放**（`domain/replay.py`、`application/replay.py`、`application/release.py`）：

- 用例绑定 `(flow_id, version)` 存库（`replay_cases`），是数据不是代码；
- 回放用不持有工具注册表的隔离执行器 `ReplayExecutor` 跑完整链路，观测步骤序列、
  工具调用（名 + 参数摘要）与终态；带副作用的工具一律按 `unconfirmed` + `replay=true` 记账；
- 断言四件事：步骤序列、工具集合、终态、以及"工具调用不得越过所在步骤的声明"（未授权工具）；
- 判定分 `pass`/`fail`/`invalid`/`unconfirmed`/`aborted`：用例写错与流程回归分开报，
  超时与崩溃一律"无结论"，谁也不降级成通过；
- 门禁（`FlowReleaseService`）先裁决再移动指针：**先识别样例、再回放**。该版本有回放
  用例时，未通过即拦下，指针不动；没有用例的版本照旧可发（回放这条），但报告里明确
  标出"没有回放依据"。

**发布前的识别样例**（`domain/intent_sample.py`、`domain/intent_eval.py`、
`application/intent_eval.py`）：

- 样例跟版本内容绑定（`FlowDefinition.intent_samples`），分正例 / 近似误例 / 超范围例；
  改样例等于改版本，避免"发布时校验的样例"与"生效的样例"两处漂移；
- 发布强制 ≥3 条正例 + ≥2 条近似误例；评测产出混淆矩阵、误路由率、漏路由率；
- **误路由必须为 0**（群众的话被塞进另一个事项的执行参数），漏路由默认上限 0；
- 超时与崩溃判 `unconfirmed`，与"通过"严格分开；报告只含 `sample_id`，不含样例原文；
- CI 入口 `scripts/evaluate_intents.py`，与启动路径共用同一套路由器装配。

细节见 [INTENT-EVAL.md](INTENT-EVAL.md) 第 3.4 节。

细节见 [SOP-REPLAY.md](SOP-REPLAY.md)。

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

### 3.7 会话与访问控制

当前阶段没有登录体系，用**签名会话令牌**代表"某个群众的一次会话"：

- 令牌格式为 `session_id.issued_at.signature`，签名为 HMAC-SHA256；
- 首次请求未带令牌时由服务端签发，通过 `X-WorkPanda-Session` 响应头下发；
- 签名不符或过期返回 `401 session_invalid`；
- 任务创建时把会话主体写入 `user_ref`，后续读写都要通过归属校验；
- 访问他人任务统一返回 `404 task_not_found`，不暴露任务是否存在；
- 确认凭证同时绑定该主体，跨会话无法消费。

令牌只是会话标识，不是实名身份，不能作为办事资格或政策判断依据。配置 `WORKPANDA_SESSION_SECRET` 后，重启不会使会话失效；未配置时使用进程级随机密钥。

### 3.8 限流

按会话主体使用令牌桶：

- `WORKPANDA_RATE_LIMIT_PER_MINUTE` 控制稳态速率；
- `WORKPANDA_RATE_LIMIT_BURST` 控制瞬时突发；
- 超限返回 `429 rate_limited` 并带 `Retry-After`。

限流状态在进程内存中，多实例部署需替换为共享存储（如 Redis）。

### 3.9 运维控制台集成

执行只走内置 harness 一条路径（见 `docs/HARNESS.md`）；控制台由 Octop 提供，只读地读取
`/api/ops/*`，详见 [OCTOP-INTEGRATION.md](OCTOP-INTEGRATION.md)。

- 早先存在的「Octop 运行时作为备选执行器」已移除：把整步委托出去后，工具循环不在本服务内，
  取消、预算、留痕、确认都只能落到整步粒度，无法证明某次副作用是否发生。这属于能力边界，
  不是配置项；
- 运维读接口 `/api/ops/*` 由按主体发放的凭据保护（`WORKPANDA_OPS_TOKENS`），
  未配置时整体 404；`viewer` 只见列表与聚合，明细要求 `admin`，见
  [OPS-ACCESS-CONTROL.md](OPS-ACCESS-CONTROL.md)；
- 控制台只读，不写业务状态，避免双写冲突。

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
| 401 | `session_invalid` | 会话令牌无效或已过期 |
| 404 | `task_not_found` | 任务不存在或不属于当前会话 |
| 404 | `flow_not_found` | 流程未注册 |
| 409 | `task_state_conflict` | 当前状态不允许该操作 |
| 409 | `invalid_transition` | 非法状态转换 |
| 409 | `confirmation_invalid` | 确认凭证不可用 |
| 422 | `request_rejected` | 参数或流程不满足约束 |
| 422 | `clarification_required` | 无法确定事项，需澄清或转人工 |
| 422 | `flow_not_published` | 流程存在但未发布 |
| 429 | `rate_limited` | 请求过于频繁，按 `Retry-After` 重试 |

## 6. 已知限制

- SQLite 调用是同步的，直接运行在事件循环中；高并发场景需改为线程池或异步驱动；
- 事件总线与限流都是单进程内实现，多实例部署需要外部共享组件；
- 会话是匿名签名令牌，不是实名认证，也没有权限分级；
- 无流程管理后台：流程定义已从库加载（版本不可变 + 发布指针 + 回滚），但编辑与发布
  仍需直接调用存储接口，运维没有操作面（Issue #51）；发布前回放已可用，但入口是
  脚本 `scripts/replay_flow.py`，不是界面；
- 意图路由是确定性规则（`domain/intent.py`），默认无规则即"识别不了→澄清"，
  尚未接入模型分类，也没有同义改写能力；
- 群众可见状态由服务端映射（`domain/presentation.py`，随 `TaskView.ui_state` 下发），
  但只到状态键与文案，组件与无障碍由前端实现；
- harness 的工具是同步执行，预算按调用次数与耗时计（不按 token），无 checkpoint 恢复；
- 尚无真实业务系统工具，外部访问能力默认关闭（`http_fetch` 需显式白名单）；
- 回放只观测"步骤要求调用哪个工具"，不覆盖"模型实际调用了几次"；回放报告不落库，
  需要留档时自行保存命令行输出；
- 审计日志已有归档端点，容量指标已有只读快照端点 `GET /api/ops/metrics`，但两者
  都未接入监控告警平台；指标中 SQLite 写锁等待与任务创建 p95 尚未埋点。容量阈值、
  扩容顺序与回滚见 [CAPACITY-RUNBOOK.md](CAPACITY-RUNBOOK.md)（阈值待确认）。

## 7. 下一步建议顺序

1. 真实身份与权限分级，替换匿名会话（决定谁能确认什么动作，是安全前提）；
2. 接入第一个真实业务系统工具（低风险、只读优先），跑通"受控工具"到业务价值的闭环；
3. 审计日志与指标（含 harness trace 的检索与聚合）；
4. 把 SQLite 替换为 PostgreSQL，并引入迁移工具；
5. 意图分类与槽位抽取的语义泛化（接入受约束模型分类）——规则版与版本化评测集已交付；
6. ~~流程定义持久化（版本、发布、停用、回滚）~~ —— 已交付（Issue #49）。后续：发布前回放
   测试（Issue #50）、新流程强制携带意图样例（Issue #52）、管理侧配置台（Issue #51）；
7. Octop 控制台插件、灰度发布。
