# 容量基线、扩容触发条件与回滚 runbook（Issue #53）

> 状态：**阈值待业务/安全责任人确认**。本文件给出的数值都是「工程初值」，不是已批准的
> 业务量承诺，也不是代码里生效的规则。服务端不会依据任何阈值自动扩容、自动切换后端
> 或自动改配置。首期真实业务的并发预期尚未给出，因此阈值按相对量/持续时长表述，
> 见第 4 节。变更阈值需在本文件记录确认人与日期。

## 1. 这份文档回答什么

「什么时候必须开 Redis / 上多实例 / 换 PostgreSQL」，而不是「怎么迁 PostgreSQL」
（后者是 Issue #34）。面向群众的公共服务迟早要有多实例与容量承诺；在此之前，
SQLite 单写者、`EventBroker` 进程内存态、进程内限流、默认 `memory` 的取消信号
都是**只在单实例下成立**的前提。判断依据必须可观测、有顺序、可回滚，不能靠感觉。

本轮交付的可执行部分：

- `GET /api/ops/metrics`：容量指标快照，含每种「采不到」的明确状态（第 3 节）；
- `WORKPANDA_EXPECTED_INSTANCES`：部署方对实例数的显式声明（第 2 节）；
- 启动期一致性门禁 `validate_deployment_topology`：声明多实例却仍用进程内状态后端时
  **启动失败**（第 2 节）。

刻意没有交付、也不假装交付的部分，见第 7 节与第 8 节。

## 2. 部署形态与启动门禁

服务无法自己数出有几个实例，因此由部署方显式声明 `WORKPANDA_EXPECTED_INSTANCES`
（默认 `1`，即当前 launchd 单实例试点）。它不是探测值，改动它属于部署操作。

| 形态 | 声明 | 允许的状态后端 | 任务存储 | 备注 |
|---|---|---|---|---|
| 单实例（当前） | `1` | `memory` / `sqlite` / `redis` | `memory` / `sqlite` | 默认。没有第二个执行者竞争 |
| 单实例 + 共享状态（过渡） | `1` | `redis` | `sqlite` | 先把 Redis 打通再扩容，隔离变量 |
| 多实例 | `>= 2` | **只能 `redis`** | **不能 `memory`** | 必须同时开启步骤租约 |
| 多实例 + PostgreSQL | `>= 2` | `redis` | PostgreSQL | 依赖 Issue #34，尚未实现 |

声明 `>= 2` 时，启动会校验三件事，任一不满足即抛 `DeploymentConfigError` 并终止启动，
**不静默降级为单实例语义**：

1. `shared_backend` 必须是 `redis`（`memory` 进程内、`sqlite` 同机共文件都不构成跨实例
   共享状态；`sqlite` 的取消信号只覆盖同机多进程）；
2. `harness_lease_enabled=true`，否则同一 `(task, step)` 可能被两个实例各执行一次；
3. `store_backend` 不能是 `memory`，否则任务与确认凭证只留在各自进程内。

门禁挂在两条启动链上：`build_container`（覆盖 `uvicorn workpanda.main:app`、
`create_app` 与测试装配）和 `build_uvicorn_config`（覆盖 `python -m workpanda`）。
选了 `redis` 而 `RedisClient.ping()` 连不上时，仍由既有的 `build_shared_state` 抛
`SharedStateError` 终止启动——**本门禁不放宽这条 fail-closed 语义**，回归测试
`test_gate_passes_but_unreachable_redis_still_fails_closed` 锁住了这一点。

门禁的边界必须说清楚：它校验的是「声明与配置是否自洽」，**不能发现运行时多起了
实例却没改声明**。多实例部署的操作清单里，「先改声明再起第二个实例」是强制步骤，
且每个实例启动后都要核对 `/api/ops/metrics` 的 `deployment.expected_instances`。
漏改声明属于流程问题，需要部署清单与人工复核兜住，代码无法替代。

## 3. 指标清单：`GET /api/ops/metrics`

请求头 `X-WorkPanda-Ops-Token`，本轮按最小暴露面**要求 `admin` 角色**（`viewer` 返回
403）；未配置任何运维凭据时整个 `/api/ops` 返回 404。是否下放给 `viewer` 属待确认
事项，见第 8 节。端点只读：不推进流程、不改状态、不调用外部服务。

响应是一份快照：`deployment`（配置枚举）、`metrics`（逐项指标）、`degraded`、
`instrumentation_gaps`、`not_applicable`、`note`。

每个指标有 `status` / `available` / `value` / `unit` / `reason` / `source`。四种状态：

| 状态 | 含义 | `value` |
|---|---|---|
| `ok` | 采到真实数值 | 数值 |
| `not_applicable` | 当前部署形态下本来就没有来源（如 `memory` 后端没有库文件） | `null` |
| `not_instrumented` | 指标已登记、但代码还没埋点 | `null` |
| `error` | 采集过程抛异常 | `null` |

**只有 `ok` 能带数值**，其余状态一律 `value=null` 且必须给 `reason`，由
`MetricValue` 的校验器强制。采集失败只上报异常类型名，不带 message（后者可能含
路径、连接串或业务值）。这条不变量直接对应验收标准「指标采集失败时接口明确报错，
不返回 0 冒充正常」：0 会被值班读成「没有压力」，而事实是「没测到」。

`degraded=true` 只在出现 `error` 时为真；`not_applicable` 是当前形态下的正常情况，
不算退化。`instrumentation_gaps` 列出尚缺埋点的指标，`not_applicable` 列出无来源的
指标——契约里始终保留这些名字，值班能看见「有这项但当前采不到」，而不是发现它
悄悄消失了。

| 指标 | 单位 | 来源 | 本轮状态 |
|---|---|---|---|
| `sse_connections` | connections | `EventBroker.total_subscriber_count()` | `ok`，**单进程口径**，多实例需按实例求和 |
| `trace_resident` | records | `len(traces)` | `ok`，留痕驻留条数（受容量上限淘汰） |
| `trace_unconfirmed_rate` | ratio | `traces.query_summary()` | `ok`，系统最关键的健康信号 |
| `database_file_bytes` | bytes | `sqlite_path.stat().st_size` | 仅 `sqlite` 后端 `ok`；否则 `not_applicable` |
| `concurrent_tool_threads` | threads | `tool_threads().live` | 仅启用 harness 时 `ok`；否则 `not_applicable` |
| `sqlite_write_lock_wait_ms` | ms | 待埋点 | `not_instrumented` |
| `task_create_p95_ms` | ms | 待埋点 | `not_instrumented` |

`trace_unconfirmed_rate` 同时是「正确性信号」而非纯容量信号：它升高时先查失败、
超时与结果未确认，不要直接当成扩容依据。

## 4. 阈值与触发动作（工程初值，待确认）

> 以下数值是**供评审的起点**，不是已批准的政策。首期真实业务并发预期缺失，因此
> 写成相对量与持续时长；确认前不得据此对外承诺容量，也不得写进代码当规则。

| 信号 | 初值（待确认） | 观察窗口 | 触发后固定动作 |
|---|---|---|---|
| `sse_connections`（单实例求和） | 持续 `> 200` | 5 分钟 | 先按第 5 节第 1 步开 Redis，再考虑加实例；不要直接加实例 |
| `trace_resident` 逼近 `harness_trace_capacity` | 持续 `>= 80%` 上限 | 10 分钟 | 先确认是留存需求还是淘汰策略问题；属观测能力，不直接触发扩容 |
| `trace_unconfirmed_rate` | `> 0.05` | 15 分钟 | 视为正确性事件：查失败/超时/外部结果未知，走人工核查，不扩容 |
| `database_file_bytes` | 单库 `> 2 GiB` 或周增 `> 20%` | 7 天 | 进入 PostgreSQL 迁移评估（Issue #34） |
| `concurrent_tool_threads.live` 接近 `harness_max_stuck_threads` | 持续 `>= 80%` | 5 分钟 | 先查卡住的工具（`docs/HARNESS.md` 第 21 节），不是加实例 |
| `sqlite_write_lock_wait_ms` | 写锁等待 p95 `> 50ms` | 5 分钟 | 埋点完成后才生效；先评估换 PostgreSQL，不要先加实例 |
| `task_create_p95_ms` | 待压测标定后另定 | 5 分钟 | 埋点完成后才生效；先定位慢在哪一段 |

顺序约束：**先开 Redis 共享后端 → 再加实例 → 最后换 PostgreSQL**。加实例不能跳过
共享后端这一步，否则每个实例都以为自己是唯一执行者；换 PostgreSQL 属于存储层迁移，
按 Issue #34 单独设计评审，不在本 runbook 里执行。

## 5. 升级操作顺序（单实例 → 多实例）

1. **打通共享状态，仍保持单实例。** 部署内网 Redis，配置
   `WORKPANDA_HARNESS_SHARED_BACKEND=redis`、`WORKPANDA_HARNESS_REDIS_URL`、
   `WORKPANDA_HARNESS_SHARED_KEY_PREFIX`、`WORKPANDA_HARNESS_LEASE_ENABLED=true`、
   `WORKPANDA_HARNESS_LEASE_TTL_SECONDS=180`（TTL 依据见 `docs/HARNESS.md` 第 22 节）。
   `WORKPANDA_EXPECTED_INSTANCES` 保持 `1`。此时门禁不拦，但 `build_shared_state`
   会探活，Redis 不可用会启动失败。
2. **单实例冒烟。** 核对 `/api/ops/metrics` 的 `deployment.shared_backend == "redis"`、
   `degraded == false`；确认 `/healthz` 与任务主链路正常。
3. **改声明并起第二个实例。** 所有实例统一设
   `WORKPANDA_EXPECTED_INSTANCES=2`、`WORKPANDA_STORE_BACKEND=sqlite`
   （跨主机多实例须先完成 Issue #34）。逐个启动，启动时核对门禁未报错，并逐个
   核对 `/api/ops/metrics` 的 `deployment.expected_instances == 2`。
4. **双实例冒烟。** 同一任务并发执行只发生一次（租约生效）；A 实例发起取消后，
   B 实例的执行能收到跨实例取消信号；重启其中一个实例后取消信号不丢。
   任一项失败立即按第 6 节回滚。
5. **观察窗口。** 按第 4 节的表持续观察，记录每一步的实际耗时与遇到的坑，
   把结果补进第 9 节走查记录。

## 6. 回滚

回滚按「先降实例、再退共享状态」的顺序，每步都要重启并复核 `/healthz` 与
`/api/ops/metrics`：

1. 停止新增实例，只留一个；**不要**先改 `WORKPANDA_EXPECTED_INSTANCES`。
2. 确认剩余实例稳定后，把 `WORKPANDA_EXPECTED_INSTANCES` 改回 `1` 并重启。
   顺序反了会让仍存活的多实例带着「单实例声明」继续跑，门禁反而失去意义。
3. 需要完全退回时，去掉 `WORKPANDA_HARNESS_SHARED_BACKEND=redis` 等配置、
   关闭租约，重启回到 `memory` 共享状态；确认取消信号与预算账本回到进程内语义。
4. 已写入 Redis 的租约、取消信号、预算账本由各自的 TTL 自然过期；不要手工删键，
   以免误删其它环境的同名前缀（用 `harness_shared_key_prefix` 区分环境）。
5. 试点部署的整体卸载/回退另见 `deploy/README.md` 的「回滚」一节；`uninstall`
   保留数据，`uninstall --purge-data` 会删除数据目录且不可恢复。

数据存储层若已迁到 PostgreSQL，回滚不能靠改配置完成，按 Issue #34 的迁移方案
单独处理。

## 7. 埋点缺口与未验证事项

`GET /api/ops/metrics` 诚实列出两项 `not_instrumented`，本轮**没有**编造数值：

- `sqlite_write_lock_wait_ms`：需在 `SqliteStore.commit` 的 `busy_timeout` /
  `sqlite3.OperationalError` 分支累计等待次数与耗时（`infrastructure/sqlite_store.py`）；
- `task_create_p95_ms`：需在 `TaskService.create_task` 出入口记录耗时分位数。

两者补齐后才能给出有依据的阈值。Issue 提到的「并发执行步骤数」本轮以
`concurrent_tool_threads.live` 近似（仅 harness 分支有台账），它与「业务步骤并发数」
不是同一个量，不要混用。

**尚未完成、不得声称通过的验证**：

- 基于真实业务并发假设的阈值标定：首期业务量假设缺失，第 4 节数值未标定；
- 双实例 + Redis 的真实冒烟（租约、跨实例取消、重启不丢）：本轮只用单进程内的
  `FakeRedisServer` 验证了装配与契约，没有跑两个真实进程；
- 按第 5 节从单实例切到双实例的 runbook 走查与实际耗时记录；
- `sse_connections` 是单进程口径，多实例总量需要外部按实例求和，端点不给全局值。

现有 `scripts/stress_tool_threads.py` 是工具线程容量与租约 TTL 的压测
（基线数字见 `docs/HARNESS.md` 第 21 节），**不覆盖** SQLite 写锁等待与任务创建
延迟；把它当作 Issue 提到的「#33 压测脚本」会高估当前基线覆盖面。

## 8. 暴露面与待确认事项

- `/api/ops/metrics` 含部署形态配置（后端类型、实例声明、租约开关）。本轮按最小
  可见范围要求 `admin`；`viewer` 是日常值班角色，是否下放需业务/安全责任人确认，
  确认后同步更新 `docs/OPS-ACCESS-CONTROL.md` 的权限矩阵与回归测试。
- 端点只输出数值、配置枚举与固定原因文案；不输出连接串、凭据、任务正文或群众
  原话。回归测试 `test_metrics_never_leak_credentials_or_citizen_text` 与
  `test_collection_error_is_reported_not_swallowed_into_zero` 分别锁住这两点。
- 第 4 节全部阈值、以及「首期真实业务并发预期」需业务责任人确认后替换初值，
  并在本文件记录确认人与日期。
- 多实例下的限流（`infrastructure/rate_limit.py` 进程内）与审计归档（单机追加文件）
  仍是单实例语义；横向扩容前需外部化，见 `docs/ARCHITECTURE.md` 第 6 节与
  `docs/HARNESS.md`。本 runbook 不宣称这两项已在多实例下成立。
- `EventBroker` 仍是进程内通知，断线重连以 Task Store 为准；多实例下 SSE 连接
  应路由到持有该订阅的实例，或改用共享推送，本轮未改动。
- `docs/ADMIN-CONSOLE.md` 与 `README.md` 里把「多实例发布同步」记为 Issue #53 承接。
  本轮只交付容量基线、扩容门禁与 runbook，**没有**实现跨实例的版本发布同步：它涉及
  发布指针与确认语义，属高风险变更，需单独设计评审。该引用在相关能力落地前仍成立，
  不要据本文件误以为已一并解决。

## 9. 走查记录模板

每次扩容/回滚后填写，作为下一轮阈值标定的依据：

| 日期 | 操作（第几步） | 耗时 | 观察到的指标 | 遇到的坑 | 结论/回滚 |
|---|---|---|---|---|---|
| 待填写 |  |  |  |  |  |

完成标准（可观察）：第 5 节每一步都有记录；双实例冒烟三项全通过；
`/api/ops/metrics` 在目标形态下无 `error`、`degraded == false`；
阈值表有确认人与日期；任一项不满足则不进入下一形态。
