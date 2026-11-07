# 通用 harness（受控执行层）

> 定位：WorkPanda 自己的能力底座。**不认识任何业务流程，也不绑定任何模型厂商。**
> 现状：已实现，并通过真实模型端到端验证（模型真实调用工具、evidence 可追溯到工具返回值）。

## 1. 为什么需要它

早期执行层只有 `Executor` 接口 + 一次模型调用：模型按提示词"空想"，`evidence` 全靠自述。
一旦模型编造，服务端无法区分"真的做了"和"说做了"——实测就出现过模型明确回复
"没有该工具、无法执行"而任务被标记为完成的事故。

harness 解决的就是这件事：**让模型能真正调用受控工具，并把每次调用变成可校验、可留痕的事实。**

## 2. 分层

```text
harness/
├── models.py         工具声明、调用、事件等公共数据结构
├── contract.py       结果契约（succeeded / failed / unconfirmed）
├── tools.py          工具协议、注册表、调用授权、限额、超时、参数校验
├── budget.py         步数、工具调用次数、耗时、token 四重预算
├── loop.py           有界执行循环（模型 ↔ 工具），可带断点续跑
├── checkpoint.py     执行快照、断点模型与存储协议
├── clients.py        模型端口 + 脚本化客户端（测试用）
├── openai_client.py  OpenAI 兼容客户端，支持 function calling
├── builtin.py        内置受控工具
├── redaction.py      落盘前脱敏（键名 + 文本模式 + 敏感值回显）
├── trace.py          执行留痕的独立存储与聚合指标
├── effects.py        副作用账本：哪些调用改动了外部世界（applied / unknown）
├── audit.py          只追加的审计归档：按大小滚动，可回读
├── cancel.py         取消信号：令牌 + 登记表（可先取消、后执行）
└── executor.py       接到应用层 Executor 端口
```

留痕与断点的持久化实现放在 `infrastructure/`：`sqlite_trace_store.py`、
`sqlite_checkpoint_store.py`、`sqlite_cancel_store.py`（跨进程取消信号）。
harness 只依赖协议，不反向依赖具体存储。

依赖方向：`executor → loop → tools/models`，全部单向；本层只依赖 `application.ports`
与自身，不反向依赖 API、存储或业务代码。

### 与 Octop 运行时的关系

Octop **不是** harness 的上层，而是它可选的一种后端：

| | 通用 harness | Octop 运行时 |
|---|---|---|
| 工具循环由谁控制 | 我们（白名单、预算、留痕） | SDK 内部，我们只能当黑盒 |
| 能否看到每次工具调用 | 能，逐次留痕 | 不能 |
| 适用场景 | 默认执行路径 | 需要 SDK 内置能力时的备选 |

装配优先级：`harness > Octop 运行时 > 脚手架执行器`（见 `bootstrap.py`）。

## 3. 十五条不变式

1. **最小可见**：模型只能看到本步骤被授权的工具子集，看不到全局注册表。
2. **四道闸**：白名单 → 限额/重复副作用 → 参数校验 → 限期执行并留痕。任一不通过一律 BLOCKED，绝不"尽力执行"。
3. **有界**：步数、工具调用次数、总耗时、模型 token 四重上限。超界一律"未确认"，因为此刻可能已调用过有副作用的工具。
4. **限期**：工具必须在 `ToolSpec.timeout_seconds` 内返回。超时记录为 `TIMEOUT`（结果未知），既不算成功也不算失败，更不允许自动重试。
5. **不重复副作用**：`idempotent=False` 的工具在一次执行内只允许成功一次；同一参数再次提交直接 BLOCKED（`duplicate_side_effect`）。
6. **可追溯**：每次模型调用与工具调用都写入 trace；摘要另存 TraceStore，可按任务检索、跨任务聚合。
7. **不允许口头成功**：本步骤有可用工具，模型却一次都没成功调用却声称 `succeeded`，降级为 `unconfirmed`（`unverified_success`）。
8. **不认识业务**：只回答"这一步要做什么"，不决定"该办什么事"。业务流程、状态、确认凭证仍在应用层与领域层。
9. **可恢复但不盲目重来**：每个工具调用落账后写 checkpoint，中断可续跑；
   只要出现过"结果未知"（工具超时），checkpoint 即标为不可续跑，续跑请求一律拒绝。
10. **副作用可长期核对**：非幂等调用必须落进只追加的审计归档（成功=已发生、
    超时=结果未知）；trace 会被淘汰，因此不能拿它当"那次提交发出去没有"的证据。
11. **只重试模型调用，绝不重试工具**：模型调用的瞬时失败（429 / 5xx / 连接被重置）
    可以有限重试，因为调用本身没有副作用；工具调用可能已经产生了副作用，只能靠
    超时/重复提交判定拦下来，不靠"再试一次"。重试次数与退避都受剩余预算约束，
    每次尝试都计入 `model_calls` 并留一条 `model_retried` 事件。
12. **并行只在幂等之间**：同一轮的多个调用，只有整批都幂等、且整批放得进剩余调用
    预算时才并发；否则整批串行。并行只改变等待方式，记录、事件与回灌顺序仍按模型
    给出的顺序——"同一副作用只发生一次"不交给调度碰运气。并发批次的预算是**原子
    预约**：要么整批扣下，要么一条都不扣；逐条扣到半途失败会留下"扣了 N 条、
    实际一条都没提交"的错账，事后按用量核对"做了几次"就对不上。
13. **取消只在边界生效**：模型调用与工具调用都是同步阻塞的，线程无法被强制打断，
    因此取消只在"下一轮模型调用之前、下一条工具调用之前、重试退避等待中"生效。
    已经提交的调用跑完并照常留痕——假装它没发生才是真正的危险。取消的结论是
    **未确认**而不是失败，断点也不清理（见第 14 节）。
14. **没做的也要留痕，但不能算做过**：预算用尽或取消让一轮调用只跑了一部分时，
    剩下的记成 `SKIPPED`（与"闸门拒绝"的 `BLOCKED` 区分开），带 `tool_skipped` 事件。
    它们不消耗调用预算、不算成功依据、不进副作用账本；trace 里 `tool_calls` 只算
    真正发起过的，省下的单独记 `skipped_tool_calls`。批次中途中止还要给它们补
    工具回执并写断点，否则续跑会拿着"assistant 声明了 N 个调用、只有 M 个结果"
    的畸形对话去请求模型。
15. **状态与承诺同生共死**：凡是决定"任务现在等谁"的写入——终态、暂停态、
    字段更新——都必须与它的事件放在同一个条件事务里，期望值取进入本次操作时
    从库里读到的状态；不符则整批丢弃并交回库里的权威状态，绝不拿旧快照覆盖，
    也绝不留下一条无主事件。纯过程事件（`STEP_STARTED` / `STEP_COMPLETED`）可以
    即时写、即时推送，因为它们不代表任何"在等谁"的承诺（见第 17 节）。

## 4. 三态映射

| 情况 | 结果 | 错误码 |
|---|---|---|
| 契约 `succeeded` + 有 `evidence` + 有成功工具调用（或本步骤无工具） | `succeeded` | — |
| 契约 `succeeded` 但无 `evidence` | `unconfirmed` | `missing_evidence` |
| 契约 `succeeded` 但有工具却零成功调用 | `unconfirmed` | `unverified_success` |
| 契约 `failed` | `failed` | `failed` |
| 工具超时（`TIMEOUT`） | 不算依据；若模型仍称 `succeeded` 则 `unconfirmed` | `unverified_success` |
| 工具被拦（`BLOCKED`） | 不算依据；原因回灌给模型 | 记录 `error_code` |
| 不是合法 JSON | `unconfirmed` | `unparsable_output` |
| 一段文本里出现两个及以上 JSON 对象 | `unconfirmed` | `unparsable_output`（不猜哪个是结论） |
| 只有推理内容、没有最终答复 | `unconfirmed` | `empty_model_response` |
| 步数 / 调用次数 / token / 耗时超限 | `unconfirmed` | `budget_max_steps` / `budget_max_tool_calls` / `budget_max_tokens` / `budget_deadline` |
| 存在断点但不允许续跑 | `unconfirmed` | `checkpoint_not_resumable` / `checkpoint_expired` / `checkpoint_mismatch` |
| 步骤已被另一执行者占用（或占用状态不明） | `unconfirmed` | `checkpoint_busy`（`stop_reason=refused`，不调模型/工具） |
| 模型超时 | `unconfirmed` | `model_timeout` |
| 模型服务错误（4xx/5xx、传输失败） | `unconfirmed` | `http_xxx` / `transport_error` |
| 连续空回复 | `unconfirmed` | `empty_model_response` |
| 执行被取消（边界生效） | `unconfirmed` | `cancelled` |

原则：**只有确定成功才报成功；不确定一律"待确认"并转人工。**

结论解析（`contract.py`）单独守两件事：

- 容忍模型多说一句话、包一层 ``` 围栏、前后带说明文字——这些不该制造出
  `unparsable_output` 的假转人工；但**文本里出现两个及以上 JSON 对象时一律拒绝**，
  因为无法判断哪个才是真正结论，猜错就可能把 `succeeded` 读进本该未确认的步骤；
- 推理模型的思考过程（`reasoning_content` / `reasoning`）与最终答复分开接收，
  只在 `model_responded` 事件里记长度与一小段**脱敏后**的预览；它不进 `text`，
  因此永远不会被契约解析器当成结论或依据。

样例按版本固化在 `tests/test_model_output.py`（全合成数据，不含真实群众信息），
覆盖明确成功、缺依据、歧义、拼写错误、超范围、注入式指令、围栏与尾随文字。
改动解析逻辑必须整体重跑，并递增 `EVAL_VERSION`。

## 5. 接入一个新工具

```python
from workpanda.harness.models import RiskLevel, ToolSpec
from workpanda.harness.tools import ToolContext, ToolError


class LookupOrderTool:
    spec = ToolSpec(
        name="lookup_order",              # 注意命名规范，见下
        description="按编号查询受理单状态。",
        parameters={
            "type": "object",
            "properties": {"order_id": {"type": "string", "maxLength": 64}},
            "required": ["order_id"],
            "additionalProperties": False,
        },
        risk=RiskLevel.LOW,
        idempotent=True,                  # 查询类幂等；有副作用必须设为 False
        timeout_seconds=5.0,
    )

    def invoke(self, arguments: dict, context: ToolContext) -> str:
        if not arguments["order_id"]:
            raise ToolError("invalid_order_id", "受理单编号不能为空")
        return "受理中"          # 失败请抛 ToolError，不要返回"错误字符串"
```

接入清单：

- [ ] 名称符合 `^[a-zA-Z][a-zA-Z0-9_-]{0,63}$`（**不要用点号**，见下）
- [ ] `parameters` 写全 `required` 与 `additionalProperties: False`
- [ ] 失败抛 `ToolError(code, message)`，不要把错误塞进正常返回值
- [ ] 有副作用的工具设 `idempotent=False`，并在流程里配确认步骤（重复提交会被拦截）
- [ ] 需要限制调用次数的工具设 `max_calls`（不设时：幂等工具只受总预算约束，非幂等工具固定 1 次）
- [ ] 凭据类入参写进 `sensitive_fields`，落盘时按字段名与值双重脱敏
- [ ] `timeout_seconds` 按真实最坏耗时设，不要一律 10 秒
- [ ] 可重试的瞬时失败抛 `ToolError(..., retryable=True)`；**非幂等工具永不重试**
- [ ] 在 `default_registry` 中注册；默认不注册 = 默认不可用
- [ ] 新增工具后补一条"未授权时不会被调用"的测试

### 参数校验覆盖到哪一层

`parameters` 里写的约束会被真正执行，不是装饰。当前支持：

| 约束 | 作用对象 | 效果 |
|---|---|---|
| `required` / `additionalProperties` | 对象 | 缺必填或多给字段一律 BLOCKED |
| `type` | 全部 | 类型不符即拒绝（`true` 不算 `integer`） |
| `enum` | 全部 | 不在取值内即拒绝 |
| `minLength` / `maxLength` | 字符串 | 长度越界即拒绝 |
| `pattern` | 字符串 | 不匹配即拒绝；schema 里的正则写错时忽略该条，不连坐合法调用 |
| `minimum` / `maximum` | 数值 | 越界即拒绝 |
| `minItems` / `maxItems` / `items` | 数组 | 元素个数与逐元素类型/长度 |
| 嵌套 `properties` / `required` | 对象 | 逐字段递归校验，报错信息带 `meta.k` 这样的路径 |

不做完整 JSON Schema：`$ref`、`oneOf`、`dependentRequired` 之类一律不支持，
需要时在工具内部校验并抛 `ToolError`。**harness 只判断"参数能不能进这个工具"，
业务规则仍属于工具自己。**

### 命名规范（实测踩过的坑）

上游 OpenAI 兼容层只接受 ASCII 字母、数字、下划线和连字符作为 function name。
最初把工具命名为 `sys.clock` / `http.fetch`，网关直接返回：

```text
400  ***.***.***.name: expected 1-128 ASCII letters, digits, underscores or hyphens
```

因此全部改为下划线风格：`sys_clock`、`http_fetch`。`ToolSpec` 的 pattern 已经强制这条规则，
新工具写错名字会在构造时就报错，而不是等到线上调用。

### 内置工具

| 工具 | 风险 | 说明 |
|---|---|---|
| `sys_clock` | 低 | 返回 UTC/本地时间与 unix 时间戳 |
| `http_fetch` | 中 | 按域名白名单抓取公开网页文本；**未配置白名单时不注册** |

`http_fetch` 的安全约束（缺一不可）：仅 https、主机白名单（精确或 `.` 前缀后缀匹配）、
解析结果必须是公网地址、不跟随重定向、限制响应体大小与超时。空白名单意味着外部访问能力
默认关闭，比默认全开安全。

### 一轮多调用的并发执行

模型一轮里返回多个工具调用时，默认仍是按序串行。`WORKPANDA_HARNESS_TOOL_CONCURRENCY`
设为大于 1 后，满足**全部**下列条件的那一批调用会并发执行：

- 批内每一条工具都声明 `idempotent=True`；
- 整批放得进剩余的工具调用预算（否则退回串行，先执行的调用仍然留痕）；
- 批内至少两条调用。

并发只改变等待方式，不改变顺序语义：记录、`tool_completed` 事件与回灌模型的消息
一律按模型给出的顺序排列；并发事实另记一条 `tools_parallel` 事件，事后按耗时排查
不会误以为它们是串行的。单条调用失败不影响同批其他调用。

接入第三方工具时要额外确认一件事：**工具自身线程安全**。沙箱里共享了状态的工具
（例如共用一个游标、一个计数器）必须保持并发度为 1，或者自己加锁——harness 不会
替它串行化。

### 上游给的调用 id 不完全可信

`call_id` 是回灌工具结果时的唯一关联键，但它是上游模型服务给的，不能直接当主键用：

| 上游实际可能给出 | 不处理的后果 | 现在的处理 |
|---|---|---|
| 两条调用共用一个 `id` | 回执串到一起，模型看到"两条调用一个结果" | 第一条原样保留，后续改名为 `<id>-2`、`<id>-3` |
| 完全不给 `id` | 构造 `ToolCall` 直接失败 | 退化为按工具名生成的 `call-<name>` |
| `id` 超过 128 字符、工具名超过 64 字符 | `ValidationError` 绕过留痕冒到调用方 | 超长部分截断 |
| 单条调用是畸形结构（非对象、空名字） | 一条坏数据丢掉整个响应 | 跳过坏条目，保留其余可用调用 |

最后一条是刻意的取舍：模型已经说了话，甚至可能已经产生副作用，丢掉整个响应等于把
"可能发生过的事"改写成"没发生过"。坏条目进不来就丢掉，能进来的照常执行。

重命名后缀必须落在长度上限**之内**：先拼后缀再整体截断，恰好 128 字符的 id 会把后缀
截掉、重命名结果退化成原 id，去重静默失效。这类"看起来生效其实没生效"的写法在
长度上限附近最容易出现，测试按边界值写而不是按典型值写。

## 6. 脱敏

留痕是为了复盘，但不能把群众信息一起留下。脱敏只在**落盘/对外**一侧生效：

| 位置 | 处理 |
|---|---|
| `result.tool_calls[].arguments` | 按键名屏蔽（含工具声明的 `sensitive_fields`） |
| `result.tool_calls[].content` | 文本模式 + 敏感值回显替换 |
| `result.trace[]` 事件 | 同上，含模型原文与工具返回值 |
| `verification` / `tool_evidence` | 同上 |
| 回灌给模型的消息 | **原样**，否则模型会拿着掩码值继续推理 |

文本模式覆盖：手机号、18 位身份证、银行卡号、`Bearer` 令牌、`sk-`/`ghp_`/`AKIA` 密钥。
已知缺口：如果工具把敏感值加工后返回（例如截断、加前缀），按值替换会失效——这类工具应自行脱敏。

### 按值遮蔽的取值口径

`content`、`trace` 这类自由文本没法按字段名定位，只能拿"这次调用里的敏感值"去文本里
逐个替换。取值范围的划法决定了兜底能力：

| 取值来源 | 漏掉的后果 |
|---|---|
| 只有工具显式声明的 `sensitive_fields` | 作者忘声明一个字段，密码就以明文落进审计记录 |
| 上面这些 **加上** 默认敏感键命中的入参 | 无（默认键即兜底） |

现在取的是并集：工具没声明 `sensitive_fields` 时，入参里叫 `password`、`token`、
`sms_code`、`id_card` 的值照样会被取出并遮蔽。理由是"忘记声明"远比"故意不声明"常见，
兜底防线不能依赖作者记得写。

取值还必须是**递归**的。按键名脱敏本来就是递归的（`{user: {password: ...}}` 的内层
密码会被遮成掩码），取值只扫顶层的话，内层密码在 `arguments` 里是 `***`、在工具回显的
`content` 里却还是明文——遮蔽只做了一半。半遮比不遮更危险：看 `arguments` 会以为已经
生效了。数组元素里的对象同样要挖。

两个执行顺序上的约束：

- 取值必须在**按字段名脱敏之前**做。先遮 `arguments` 再取值，取到的就是掩码本身，
  `content` 里的明文再也替换不掉。
- `redactor=NO_REDACTION`（显式关闭）时这条路径整体跳过，包括按值遮蔽。关闭了却还在遮，
  会让运维以为看到的是真实值、其实看到的是掩码，排查时对不上账。

## 7. 留痕与指标

每次执行生成一个 `trace_id`，摘要写入 `TraceStore`：

- `GET /api/ops/traces?task_id=&limit=`：按任务检索，新的在前；
- `GET /api/ops/traces/summary`：聚合指标——**未确认占比**是首要健康信号，
  其次是被拦调用数、失败调用数、超时调用数、被跳过调用数、平均模型调用轮数与
  token 总量。
- `GET /api/ops/tool-runtime`：工具线程台账（`live` / `stuck` / `max_stuck`），
  `stuck` 是"已超时但线程还没退出"的数量。接近上限时新调用会被拒绝（第 19 节）。

`tool_calls` 的口径是"**真正发起过**的调用次数"：被预算用尽或取消省下的调用会以
`SKIPPED` 记录留在任务事件里（含名称、参数与原因），但只计入 `skipped_tool_calls`，
不混进 `tool_calls`。两种存储后端对这个字段的读写与聚合口径一致，SQLite 老库会自动
补 `skipped_tool_calls` 列，旧记录按 0 处理。

存储后端由 `WORKPANDA_HARNESS_TRACE_BACKEND` 决定：

| 取值 | 存储 | 适用 |
|---|---|---|
| `memory` | 进程内，有界 500 条 | 本地开发 |
| `sqlite` | 与任务库同文件的 `harness_traces` 表 | 单机试点，重启后仍可查 |
| `auto`（默认） | 跟随 `WORKPANDA_STORE_BACKEND` | 不希望多配一个开关时 |

两种实现的聚合口径一致（同一组记录算出的未确认占比、均值、超时数相同），
可以按需切换而不破坏指标可比性。`harness_trace_capacity` 对两种后端都生效，
`0` 表示不淘汰——只建议在有人负责清理时使用。

`trace` 只描述"执行层发生了什么"，业务任务状态仍以 Task Store 为准，两者不能互相替代。
`trace` 会被容量淘汰，因此**不能**拿它回答"某次提交到底发出去没有"——那件事由
第 13 节的审计归档负责。

## 8. 断点续跑

一个步骤可能跑多轮模型、多个工具。进程在中途被重启时，此前发生的工具调用既不能重来
（可能重复副作用），也不该整段丢弃（用户已经确认过的动作不该再走一遍）。

做法：每个工具调用落账后，把"对话历史 + 已发生的调用 + 预算用量 + 授权状态"写成
一份 checkpoint。再次执行同一步骤时，执行器先找断点：

1. 流程/版本/步骤或授权工具集合不一致 → 拒绝（`checkpoint_mismatch`）；
2. 断点里出现过工具超时**或崩溃** → 拒绝（`checkpoint_not_resumable`）；
3. 超过 `harness_resume_ttl_seconds` → 拒绝（`checkpoint_expired`）；
4. 全部通过 → 从断点继续，并把上次的 `trace_id` 记入新 trace 的 `resumed_from`。

被拒绝时结果是 `unconfirmed`，`stop_reason=refused`，**不会重新调用模型或工具**。
这是刻意的：外部系统可能已经执行过，只有人工能确认。此时断点会保留，需要人工核实后
按下文清理，否则该步骤会持续被拒绝。

**崩溃与超时同属"不可续跑"。** 工具抛出未预期异常时，请求可能已经送达外部、只是
读响应时崩了——续跑就会把同一笔提交再发一次。因此断点的 `blocked_reason` 会记成
`tool_crashed`，与 `tool_timeout` 一样落进拒绝分支。两者的差别只在归档里看得见：
超时说明"等不到回应"，崩溃说明"工具自己没说完话"。

断点的生死规则：模型给出结论（`completed`）后立即清理；模型错误、超预算这类"没有结论"
的结束则保留，供下一次续跑或明确拒绝——绝不把一段未完成的执行当成没发生过。

续跑时恢复的不只是对话，还有 `ToolGrant` 的执行期状态（各工具已用次数、已成功副作用
指纹）。因此已经提交过的非幂等操作会被拦成 `duplicate_side_effect`，副作用不会发生第二次。
预算也接着算：断点续跑不会凭空多出一份步数、调用次数或 token 配额。

续跑产生的新 trace 通过 `resumed_from` 指向来源，并**只统计这次尝试发生的调用**；
断点里带过来的记录仍作为 `tool_evidence` 依据，但不重复计入调用次数。

开启与清理：

```bash
export WORKPANDA_HARNESS_RESUME_ENABLED=true
# 断点与任务槽位同库同权限；确认无误后清理某一步骤的断点：
sqlite3 workpanda.db "DELETE FROM harness_checkpoints WHERE task_id='...' AND step_id='...'"
```

**默认关闭**。开启后 checkpoint 里保存的是**未脱敏**的对话原文（脱敏值会让模型拿着掩码
继续推理），因此必须先确认该库的权限、备份与留存策略可接受。

## 9. 配置

| 变量 | 默认值 | 说明 |
|---|---|---|
| `WORKPANDA_HARNESS_ENABLED` | `false` | 启用通用 harness 执行器 |
| `WORKPANDA_MODEL_BASE_URL` | 空 | OpenAI 兼容接口地址，如 `http://host/v1` |
| `WORKPANDA_MODEL_API_KEY` | 空 | 模型凭据 |
| `WORKPANDA_MODEL_NAME` | `gpt-4o-mini` | 模型标识 |
| `WORKPANDA_MODEL_TIMEOUT_SECONDS` | `60` | 单次模型调用超时 |
| `WORKPANDA_MODEL_RETRIES` | `1` | 模型调用瞬时失败的额外尝试次数；`0` 为不重试 |
| `WORKPANDA_MODEL_RETRY_BACKOFF_SECONDS` | `0.5` | 重试退避基数，第 n 次重试等 `n × 基数` 秒 |
| `WORKPANDA_MODEL_TRUST_ENV` | `true` | 是否沿用系统代理变量；内网地址被代理拦截时置 `false` |
| `WORKPANDA_HARNESS_MAX_STEPS` | `6` | 单步骤最多几轮模型调用 |
| `WORKPANDA_HARNESS_MAX_TOOL_CALLS` | `8` | 单步骤最多几次工具调用 |
| `WORKPANDA_HARNESS_MAX_TOKENS` | `0` | 单步骤 token 上限；0 为不限，仅在上游上报 usage 时生效 |
| `WORKPANDA_HARNESS_DEADLINE_SECONDS` | `45` | 单步骤总耗时上限（同时是单次工具调用的兜底超时） |
| `WORKPANDA_HARNESS_TOOL_RETRIES` | `1` | 可重试失败的额外尝试次数，仅对幂等工具生效 |
| `WORKPANDA_HARNESS_TOOL_CONCURRENCY` | `1` | 一轮内最多几个幂等工具并发执行；`1` 为串行 |
| `WORKPANDA_HARNESS_MAX_STUCK_THREADS` | `16` | 允许"已超时但线程仍未退出"的个数上限；触顶拒绝新调用，`0` 为关闭闸门 |
| `WORKPANDA_HARNESS_REDACT` | `true` | 落盘脱敏开关；除排障外不要关闭 |
| `WORKPANDA_HARNESS_TRACE_CAPACITY` | `500` | trace 保留条数上限；0 为不淘汰（两种后端都生效） |
| `WORKPANDA_HARNESS_TRACE_BACKEND` | `auto` | `memory` / `sqlite` / `auto`（跟随 store_backend） |
| `WORKPANDA_HARNESS_RESUME_ENABLED` | `false` | 断点续跑；开启后每个工具调用都会落 checkpoint |
| `WORKPANDA_HARNESS_RESUME_TTL_SECONDS` | `900` | 断点保留时限，超时拒绝续跑 |
| `WORKPANDA_HARNESS_LEASE_ENABLED` | `false` | 启用步骤租约（同一 task+step 同时只允许一个执行者） |
| `WORKPANDA_HARNESS_LEASE_TTL_SECONDS` | `0` | 租约时长；`0` 表示按 `deadline + 90s` 自动推导 |
| `WORKPANDA_HARNESS_CANCEL_BACKEND` | `memory` | 取消信号可见范围：`memory` 仅本进程 / `sqlite` 同机多进程共享 |
| `WORKPANDA_HARNESS_CANCEL_TTL_SECONDS` | `600` | 共享取消信号有效期；过期不再拦住同一任务的后续执行 |
| `WORKPANDA_HARNESS_CANCEL_POLL_SECONDS` | `0.1` | 退避等待里轮询共享信号的粒度 |
| `WORKPANDA_HARNESS_ALLOWED_HOSTS` | 空 | 逗号分隔的 http 工具域名白名单 |
| `WORKPANDA_HARNESS_AUDIT_PATH` | 空 | 审计归档文件路径；为空则不归档 |
| `WORKPANDA_HARNESS_AUDIT_MAX_BYTES` | `5000000` | 单个归档文件大小上限，超过即滚动 |
| `WORKPANDA_HARNESS_AUDIT_BACKUPS` | `3` | 滚动保留的备份份数；`0` 表示只留当前文件 |

`HARNESS_ENABLED` 为 true 且模型三项配置齐全时才启用；否则回落到 Octop 运行时或脚手架执行器。

## 10. 验证

```bash
pytest tests/test_harness.py tests/test_checkpoint.py tests/test_trace_store.py \
       tests/test_openai_client.py tests/test_redaction.py tests/test_trace.py \
       tests/test_effects.py tests/test_audit.py tests/test_cancel.py
python scripts/smoke_harness.py     # 真实模型端到端
```

`smoke_harness.py` 做四个硬断言：

1. 声明了 `sys_clock` 的步骤 → 模型必须真的调用它，成功且 `tool_evidence` 可追溯；
2. 工具未注册的步骤 → 只能如实报告，不能报完成；
3. 失败必须留痕（模型原文进入任务事件）；
4. 每次执行都要落一条可检索的 trace，且不能被整体判成"未确认"。

## 11. 已验证与未验证

已验证（全量 `pytest` 通过、`ruff check .` 全通过）：

- 白名单拒绝、参数校验、工具异常、风险归并；
- 敏感值回显遮蔽在工具**未声明** `sensitive_fields` 时仍然生效（默认敏感键兜底），
  声明了自定义字段名同样遮蔽；嵌套对象与数组元素内的敏感值也会被取出并遮蔽；
  `NO_REDACTION` 下按值遮蔽确实停用；
- 上游调用 id 的四种坏情况都不再污染回执：重复 id 改名为 `<id>-2`、缺 id 退化为
  `call-<name>`、超长 id/工具名截断而不抛错、单条畸形调用只丢自己；恰好 128 字符
  的重复 id 仍能拿到两个不同键（后缀不被截断吃掉）；
- 模型输出契约评测集（`EVAL_VERSION = 2026-11-07.1`，16 组合成样例）：围栏、
  前后说明文字、尾随花括号都能解析；两个 JSON 对象、拼写错误、注入式指令、
  超范围回复一律不误报成功；推理内容只留脱敏预览，不能当结论或依据；
- 参数约束真的执行：越界数值/长度、不匹配正则、数组元素个数与元素本身、嵌套对象
  必填与多余字段都被拒；schema 里的正则写错时不连坐合法调用；
- 模型耗时按**单次往返**记录：慢的第一次调用不会把耗时带进第二次，失败路径（超时、
  传输错误）不产生耗时；拒绝续跑的路径同样记录真实耗时，不再写死为 0；
- 工具超时按声明值掐断（0.1 秒声明、0.5 秒实际耗时，实测在 0.1 秒级返回 `TIMEOUT`）；
- 可重试失败对幂等工具重试一次并成功；非幂等工具永不重试；
- 模型调用瞬时失败（429 / 5xx / 传输错误）重试一次后成功；4xx 鉴权与参数错误不重试；
  重试次数有上限，剩余预算放不下退避时间时直接上报原错误；重试不会重放已发生的工具调用；
- 非幂等工具重复提交被拦（`duplicate_side_effect`），副作用实际只发生一次；
- 单工具调用上限（`max_calls`），并发调用同样守得住（限额判定与尝试计数在同一临界区）；
- 一轮返回多个工具调用时按序执行，每条结果都回灌给模型；
- 幂等工具的一轮多调用可真并发：执行窗口重叠、记录与回灌顺序仍按模型给出的顺序；
  批次里只要有一条非幂等、或整批放不进剩余调用预算，整批退回串行；
  单条调用失败不影响同批其他调用；并发事实以 `tools_parallel` 事件留痕；
- 四类预算耗尽（步数、调用次数、token、耗时）均映射为未确认；
- 并发批次的预算预扣是原子的（全成或全不成）：截止时间已耗尽时整批一条都不扣、
  一条都不执行，并按 `SKIPPED` 留痕；不会出现"扣了 N 条、只提交 M 条"的错账；
- 批次中途中止（预算耗尽 / 取消）时，没轮到的调用记成 `SKIPPED`：不执行、不耗预算、
  不算副作用、不进审计 effect；断点里的 `assistant.tool_calls` 与工具回执一一对应，
  续跑不会把畸形对话发给模型；
- 敏感入参落盘被掩码，同一值回灌给模型时保持原样；
- trace 成功/未确认均落盘，倒序检索、按任务过滤、容量有界、聚合指标正确；
- trace 落 SQLite 后跨重启可查、与内存实现聚合口径一致、老库自动补列；
- 断点续跑：崩溃后从断点继续，模型重复要求同一提交被拦，副作用仍只发生一次；
- 断点在工具超时后标为不可续跑，续跑请求直接拒绝且不调用模型或工具；
- 授权工具集合不一致、超过 TTL 的断点同样拒绝；续跑不重置步数/调用/token 预算；
- 正常结束后断点被清理；未开启续跑时不产生任何断点落盘；
- 步骤租约：同键第二个执行者被拒（`checkpoint_busy`）且不调模型/工具；
  SQLite 实现跨连接互斥、TTL 过期可接管；成功与未确认结束都释放租约；
  租约存储异常时按拒绝处理（fail-closed）；取消先于租约；
- 运维接口 `/api/ops/traces` 与 `/summary`：未配置 404、无凭据 401；
- 审计归档：成功提交记为 `applied`、超时记为 `unknown` 并置 `requires_manual_check`，
  敏感入参不出现在归档文件里（只留 16 位参数指纹）；
- 归档按大小滚动且备份份数有界、坏行不致整份失读、并发追加不撕裂行；
- 归档回读跨当前文件与全部滚动备份（新的在前）、`limit` 跨文件合计、
  `task_id` 过滤在备份中同样生效，当前文件缺失时仍能读备份；
- 归档写入失败不改变执行结论（磁盘满时任务仍判 succeeded）；
- 运维接口 `/api/ops/audit`：配置归档后可按任务检索，未配置返回空列表；
- 真实模型（内网 `deepseek-v4-1-flash`）下工具真实执行：`model_calls=2`，
  `evidence` 即 `sys_clock` 返回的时间戳。
- 真实模型（阿里云百炼 `deepseek-v4.1-flash-1017`）下跑通"调用非幂等工具 → 归档"：
  `model_calls=2`、工具真实执行 1 次，归档得到 1 条 `effect(applied)` + 1 条 `execution`，
  `requires_manual_check=false`，敏感入参只留指纹、文件里查不到原文；
- 执行进行中收到 HTTP 取消：取消请求在 1 秒内返回、执行线程在同一轮里看见信号，
  迟到的执行结果不会把 `cancelled` 覆盖回 `completed` 或 `outcome_unconfirmed`；
- 事件总线从工作线程发布仍能送达订阅者（SSE 队列按订阅时的事件循环投递）；
- 跨进程取消信号：两个独立 SQLite 连接 / 两个独立容器之间，取消前与开始后写入的信号
  都能在下一个边界被读到；退避等待 5 秒被 0.1 秒级的跨进程取消打断；信号过期后不再生效；
- 共享信号写失败返回 503 `cancel_not_delivered` 且任务状态不翻转；读不出来按"已取消"
  fail-closed 停下；未知后端配置在启动时直接报错，不静默退化。

未验证：

- 真实业务系统工具（尚未接入任何外部 API）；
- 超时后遗留线程在真实长任务下的资源影响：现在有台账与容量闸门（第 19 节），
  但上限该取多少，只有在真实长任务压力下量过才算数；
- 跨主机多实例下的预算与限流（预算仍在单进程内累计）；
- 跨主机多实例下的步骤租约与取消信号：本实现只覆盖同机多进程（SQLite）与同进程并发；
- 执行结束与持久化之间那段窗口已由第 17 节的条件写关掉（含暂停与回答分支）；
  仍未覆盖的是**跨主机**多实例在同一窗口上的仲裁。

## 12. 已知限制

- 工具是同步执行。超时用"独立线程 + 放弃等待"实现，**线程本身无法被强制终止**，
  会继续跑到自然结束——所以工具自身仍必须自带超时与幂等。这类线程现在会被记账，
  触顶后拒绝再起新的（第 19 节），但那只是"不再变坏"，不是"把线程收回来"；
- 并发执行只在幂等工具之间发生，harness 只保证调用顺序语义，不保证工具线程安全；
  非幂等工具即使并发度大于 1 也始终串行；
- token 预算依赖上游返回 `usage`；网关不返回时该项不生效，也不会用字符数估算；
- 耗时上限按**每次尝试**计，步数/调用次数/token 按整个步骤累计：否则一次中断会让
  剩余时间永久不足，步骤再也跑不完；
- checkpoint 假设同一 (task, step) 只有一个写者；开启第 15 节的步骤租约后可挡住
  同机多进程并发，跨主机部署仍需共享锁；
- checkpoint 保存未脱敏的对话原文，只应与任务槽位同库同权限存放；
- 被拒绝的断点会保留，需要人工核实后按第 8 节清理，否则该步骤持续被拒绝；
- 事件总线与限流器仍在单进程内（事件总线已支持从工作线程安全发布），横向扩容前需外部化；
- 回答与拒绝确认输掉条件写时按冲突收口（409 `task_state_conflict`）：客户端应刷新
  重取任务后再决定下一步，不要自动重放同一请求——任务可能已被取消或转人工，
  重放只是再失败一次，还会在日志里留下两笔同义的尝试；
- TraceStore 有界（默认 500 条），超出的旧记录会被淘汰：适合排障，不适合作为审计归档；
  要长期核对副作用请开启审计归档（第 13 节），两者不能互相替代；
- 审计归档是单机追加文件：多实例同时写需要共享文件系统或换成集中式日志；
  `read` 会连同滚动备份一起检索，但只覆盖本机 **保留中** 的那几份备份，
  超出 `BACKUPS` 份数被淘汰的旧文件无法找回，需要长期留存请同步外部日志。

## 13. 审计归档与副作用账本

trace 会被容量淘汰，任务事件随任务归档，两者都不能回答一个具体问题：
**三个月前那次提交，到底发出去没有？** 只要流程里出现非幂等工具（提交申请、发出材料），
这个问题就必须有长期答案，否则事故只能靠人回忆。

### 两层职责

| 模块 | 职责 | 边界 |
|---|---|---|
| `harness/effects.py` | 判定"哪些调用改动了外部世界"，产出 `applied` / `unknown` | 纯函数，不落盘 |
| `harness/audit.py` | 把执行结论与副作用追加到只追加文件，并提供回读 | 只管写与读 |

### 判定口径（宁可多一条待核对，也不要漏一次提交）

| 情况 | 结论 |
|---|---|
| 非幂等工具成功 | `applied`——外部系统确定执行过 |
| 非幂等工具超时 | `unknown`——可能执行过，必须人工核对 |
| 非幂等工具**崩溃**（未预期异常，非 `ToolError`） | `unknown`——工具没能把话说完，外部状态未知 |
| 非幂等工具被拦（未授权 / 参数非法 / 重复提交） | 不入账——确定没执行 |
| 非幂等工具自报失败（`ToolError`） | 不入账——工具明确说没成 |
| 幂等工具（查询类），含其超时与崩溃 | 不入账——查询不改变外部状态 |
| 工具不在登记表里 | 按"可能有副作用"处理——登记表落后时保守对待 |

**"崩溃"和"自报失败"必须分开。** 工具自己抛 `ToolError` 是它明确说了"我没成"，
那是确定没执行；而连接重置、响应体解析失败这类未预期异常，根因恰恰发生在请求
已经发出去之后——工具没能给出结论，外部状态就是未知的。把它当成"确定没执行"，
事后就再也没人知道那笔提交到底成没成。这类记录统一带 `CRASH_ERROR_CODE`
（`tool_crashed`），账本据此按 `unknown` 入账，断点据此拒绝续跑，两处口径同源。

一次执行只要出现一笔 `unknown`，执行记录里 `requires_manual_check` 即为 `true`。
这是刻意的：**结果未知不是失败，也不是成功，只能交给人。**

### 归档内容

- 一条 `execution`：结论、停止原因、错误码、风险、模型/工具调用数、token、耗时、
  副作用计数与 `requires_manual_check`；
- N 条 `effect`：工具名、状态、风险、调用 id、**参数指纹**（sha256 前 16 位）、错误码。

归档**不写**模型原文，也**不写**工具参数原文：参数只留指纹，能回答"两次调用是不是同一个
动作"，但不会把群众信息写进一个长期保留的文件。

### 开关与运维

```bash
export WORKPANDA_HARNESS_AUDIT_PATH=/var/lib/workpanda/audit.jsonl
```

- 默认关闭（路径为空即不归档），避免无意中把执行痕迹写到意外位置；
- 每次写入都 `fsync`：归档的价值全在"进程被 kill 之后还查得到"；
- 超过 `HARNESS_AUDIT_MAX_BYTES` 即滚动为 `.1`、`.2`…，最多保留 `BACKUPS` 份；
- 查询：`GET /api/ops/audit?task_id=&limit=`（未配置归档时返回空列表，不是 404）；
  回读会从当前文件起、按 `.1`→`.2`… 由新到旧一并检索，`limit` 是跨文件合计的上限；
- 写入失败只影响留痕，不影响执行结论——已经办成的事不会因为磁盘满被改判为失败。

## 14. 取消

取消和超时、预算耗尽常被混为一谈，但语义完全不同：超时是"不知道外部发生了什么"，
预算耗尽（`budget_*`）是"时间/次数用完了"，取消是"人不要这个结果了"。三者共同的
底线是都不能当成功，也都不能自动重来。

### 为什么不能只改任务状态

只把任务状态改成 `cancelled` 是账面上的事，正在跑的执行根本不知道：它会把剩下的
模型轮次与工具调用做完，包括那笔用户刚说"不要了"的提交。取消必须是一个执行侧
看得见的信号。

```python
from workpanda.harness.cancel import CancelRegistry

registry = CancelRegistry()          # 装配层持有，取消方与执行方共用同一个对象
registry.cancel_task(task_id, "user_cancelled")   # 从 HTTP 线程调用
```

`cancel_task` 返回被置位的执行数；返回 `0` 不代表什么都没发生——执行可能还没开始，
此时取消会**暂存**下来，等这一步真正开始取令牌时立刻生效（`begin` 时消费）。
暂存的取消有 TTL（默认 600 秒）与条数上限，过期即失效：一个早就结束的任务不该被
一条陈旧的取消永久拦住。

### 生效边界（这是全部）

| 边界 | 行为 |
|---|---|
| 执行开始前 | 一步都不跑，连模型都不调，结论 `unconfirmed` + `cancelled` |
| 模型返回调用清单、工具尚未执行 | 整批一条都不执行，`skipped_calls` 记录被省下的调用 |
| 串行批次里第 N 条之前 | 前 N-1 条照常执行并留痕，后面不执行 |
| 重试退避等待中 | 立即停止等待，不再请求模型 |
| 模型调用 / 工具调用进行中 | **不打断**，等它自己结束，结果照常落账 |

最后一行是刻意的：工具线程无法被安全杀死，硬杀等于把"是否已提交"变成未知。
所以取消从不承诺"立刻停"，只承诺"不会再有新的动作"。每条取消都留一条
`cancelled` 事件，带 `stage`（`before_model` / `before_tool` / `retry_backoff`）
与 `reason`——事后核对"取消之后还有没有发出去什么"，看这一条就够。

### 结论与断点

- 结论一律 `unconfirmed`，错误码 `cancelled`，并进入 trace 与审计归档
  （`stop_reason=cancelled`）；
- 断点**不清理**：用户取消的是"继续跑"，不是"刚才那笔没发生"。续跑时
  `duplicate_side_effect` 仍然生效，已提交过的非幂等动作不会被重复提交；
- 取消不产生"已交付"的假象：只要有工具成功调用过，就仍然需要人工核对。

### 并发前提（已落地，见第 16 节）

取消要真正有用，执行必须在不阻塞请求线程的地方跑。早期执行是同步调用，若部署在
单进程单事件循环上，取消请求本身要排队到执行结束才被处理——那时已经没有可取消的
东西了。现在会推进流程的三个接口都放到工作线程，取消接口留在事件循环上；多实例
部署时可把取消信号切到 SQLite 共享后端。剩下的边界不变：取消仍只在下一个边界生效，
已经提交的调用仍会跑完并照常留痕。

### 已验证

- 令牌第一个原因生效、重复取消不覆盖；退避等待可被取消打断（5 秒等待在 0.1 秒内返回）；
- 登记表按任务隔离，取消不跨任务扩散；执行结束即释放令牌，登记表不随任务数增长；
- 先取消后执行：暂存的取消在取令牌时生效，且只读查询不消费它；
- 循环在 `before_model` / `before_tool` / `retry_backoff` 三个边界停下，停后不再请求模型；
- 串行批次里第一条工具取消 → 第二条不执行，但第一条的记录与事件完整保留；
- 并发批次已提交的幂等调用全部留痕，停发生在下一轮模型调用之前；
- 执行器在开始前被取消时不调模型、不碰工具，仍写 trace 与审计；
- 执行中被取消：非幂等工具只执行一次，结论未确认，断点保留且可续跑；
- 取消接口只翻转状态的时代结束：`POST /api/tasks/{id}/cancel` 会把信号交给执行侧；
- 执行进行中收到 HTTP 取消的并发场景、跨进程信号与失败语义见第 16 节。

### 未验证 / 已知限制

- Octop 运行时与脚手架执行器不读这个信号：它们的"开始前"这一道由第 18 节的包装器
  统一兜住，但执行进行中仍只能跑完（取消对它们不是全无效，只是只覆盖四道边界里的第一道）；
- 跨主机多实例仍需把共享信号后端换成 Redis / advisory lock，SQLite 只覆盖同机多进程；
- 执行结束到结果持久化之间极窄的窗口内收到取消，仍可能先落一条结果事件（第 16 节）。

## 15. 步骤租约（单写者保护）

checkpoint 假设"同一 (task_id, step_id) 只有一个写者"。两个执行者同时读到同一份
断点时，防重复副作用的指纹是各自内存里的 `ToolGrant` 状态，彼此看不见；只要这一步
有非幂等工具，外部动作就可能真的发生两次。`harness/lease.py` 在断点之外补一层
极小的互斥：

- `execute()` 开始时抢一个占位租约（随机 owner + TTL），**抢不到或状态不明一律拒绝**：
  结果 `unconfirmed`、`checkpoint_busy`、`stop_reason=refused`，不调模型、不调工具、
  不留断点，但仍写 trace 与审计；
- 执行结束（成功、未确认、异常）在 `finally` 里释放。未确认时释放是刻意的——
  断点还在，后续那一次执行本就应该能接着跑；
- 取消先于租约：一步都不跑时不需要占用身份；
- 租约**默认关闭**，多进程试点时由 `WORKPANDA_HARNESS_LEASE_ENABLED=true` 开启，
  后端跟随任务存储（SQLite 与任务库同文件，同机跨进程互斥；内存后端只挡同进程并发）。

TTL 必须长于一次执行的最坏耗时（步骤 deadline 之外还有模型/工具超时与重试退避），
否则租约会在执行中途过期，等于没保护。默认按 `deadline + 90s` 推导，实际部署应
按真实超时显式设置 `WORKPANDA_HARNESS_LEASE_TTL_SECONDS`。进程崩溃时租约靠 TTL
过期，人工重试才不会被永久挡住。

边界要说清楚：**跨主机多实例部署仍需把这里的 SQLite/内存实现换成共享锁**
（Redis、数据库 advisory lock 等），单写者假设本身不变。开启后仍保留的其他
限制：预算与限流仍在单进程内、事件总线未外部化。

## 16. 让取消真的落地：执行离环与跨进程信号

第 14 节写了取消的三个边界，但那时有两个前提没兑现：执行占着事件循环，取消请求
排不上队；取消信号只在自己进程的内存里，另一个 worker 里的执行根本看不见。
这一节补的就是这两个前提，**不改取消本身的语义**——仍不打断已提交的调用、
结论仍是"未确认"、断点仍保留。

### 执行离环

`POST /api/tasks`、`/answers`、`/confirm` 都会推进流程，可能真的调用模型与工具，
耗时可达数十秒。三者改用 `asyncio.to_thread` 在工作线程跑；`POST /cancel` 刻意
留在事件循环上，只做状态翻转与信号下发，因此执行进行中也能立刻被处理。

配套两件事：

- `EventBroker.publish` 记住订阅者所在的事件循环，从工作线程发布时用
  `call_soon_threadsafe` 把投递排回那个循环——`asyncio.Queue.put_nowait` 不是线程安全的。
  推送丢了也不丢事件，SSE 断线重连以 Task Store 为准；
- `FlowRunner` 拿到执行结果后重读一次任务状态。若已被取消，直接返回那条
  `cancelled` 任务，**不写结果事件**：一次迟到的执行结果不能把用户刚点的取消
  覆盖回 `completed` / `failed` / `outcome_unconfirmed`。

### 共享取消信号

`CancelRegistry` 可接一个 `CancelSignalStore`（`signal` / `reason_for`）。
默认仍是 `memory`，行为与之前完全一致；配 `WORKPANDA_HARNESS_CANCEL_BACKEND=sqlite`
后信号写到任务库同文件的 `harness_cancel_signals` 表，同机多进程可见：

| 时刻 | 行为 |
|---|---|
| 取消请求 | 先写共享信号，再置位本进程令牌；写失败抛 `CancelSignalError` |
| 边界检查（`cancelled` / `raise_if_cancelled`） | 主动拉一次共享信号，发现取消即置位令牌 |
| 重试退避 | 按 `CANCEL_POLL_SECONDS` 切片等待，每片拉一次，不等完整退避 |
| 共享信号读不出来 | 按 `cancel_signal_unavailable` **已取消**收口：状态不明时不再产生新动作 |
| 信号过期 | 不再拦住同一任务的后续执行；人工确认后可直接删表记录 |

**信号写不成功时不能假装成功**：取消接口返回 503 `cancel_not_delivered`，
任务状态不翻转，调用方应重试。多进程部署下界面显示"已取消"、另一个进程却还在
提交，比一次可重试的失败危险得多。`CancelSignalStore` 只是协议，跨主机部署
换成 Redis / 数据库 advisory lock 即可，执行侧的边界语义不用改。

```bash
export WORKPANDA_HARNESS_CANCEL_BACKEND=sqlite
# 人工确认后清理一条陈旧信号，让同一任务可以重新执行：
sqlite3 workpanda.db "DELETE FROM harness_cancel_signals WHERE task_id='...'"
```

### 仍然没有承诺的事

- 取消依旧不打断正在进行的模型/工具调用，只保证"不会再有新的动作"；
- SQLite 只解决同机多进程；跨主机多实例的取消、预算与限流仍需外部化；
- `asyncio.to_thread` 用的是默认线程池，并发步骤数受池大小约束，这是有意的背压；
- 结果守卫读状态之后、结果持久化之前那段极窄窗口，已由第 17 节的条件写消除：
  取消与结果落地共用一次状态 CAS，状态与结果事件同生共死，谁先写谁生效；
- Octop 运行时与脚手架执行器仍不读这个信号。

## 17. 条件写：让取消与迟到结果真正互斥

第 16 节把执行挪出了事件循环，取消才第一次能在执行进行中被处理。但那之后还剩
一个更窄、也更难复现的窗口：`FlowRunner` 拿到执行结果后重读一次状态，确认没被
取消才写结果；从"读"到"写"之间，取消请求完全可能插进来——旧实现两边都是
**无条件覆盖**，于是取消方写下的 `cancelled` 会被迟到结果盖回 `completed`，
而结果事件已经先落了一条。这一节把这段窗口关掉。

### 为什么只加一次状态守卫不够

守卫只能挡住"读之前已经发生的取消"。真正的并发里，另一个写者可以在守卫
返回之后、`save` 之前动手：

| 时刻 | 执行侧（工作线程） | 取消侧（事件循环） |
|---|---|---|
| t1 | `get()` 读到 `running` | |
| t2 | | 信号下发成功，写 `cancelled` + `CANCELLED` 事件 |
| t3 | 写结果事件、把状态覆盖回 `completed` | |

两边各自看自己的快照都"没毛病"，但库里最后是错的。要互斥就得让**判定和写入
是同一个原子动作**，而不是"先判定、后写入"两步。

### `TaskStore.commit`：状态与事件同生共死

新增的原子原语是 `commit(task, events, *, expected_status)`：

- `expected_status` 是调用方进入本次推进时从库里读到的状态快照；
- 仅当库里当前状态仍等于它时，才在**同一个事务**里追加 `events` 并写入任务；
- 不符则整笔回滚，一行状态、一条事件都不落，抛 `StaleTaskState`；
- 返回已落盘事件，调用方在提交成功后才发布 SSE 推送。

`events` 是 `PendingEvent(type, status, data)`：`status` 由调用方显式给出，
因为同一次提交里不同事件对应不同阶段（例如 `execution_failed` 发生在
`FAILED` 之前），存储层不能拿最终状态代猜。

SQLite 实现的顺序是关键：先执行带 `AND status = ?` 的 `UPDATE`，命中后即
持有写锁直到 `commit()`，随后分配顺序号、插入事件都在同一临界区里。**不能
先 SELECT 再 UPDATE**——那中间恰好是另一个进程翻状态的窗口，等于白做。
内存实现在同一把锁内先校验再写，语义一致。

### 双方共用一次仲裁

| 路径 | 期望值 | 提交内容 | 条件写输了 |
|---|---|---|---|
| 执行结果落地 | 进入 `advance()` 时读到的状态 | `COMPLETED` / `FAILED` / `OUTCOME_UNCONFIRMED` / `HANDOFF_REQUIRED` 及其诊断事件 | 丢弃整批结果，返回库里的权威状态 |
| 暂停（等输入 / 等确认） | 进入 `advance()` 时读到的状态 | `waiting_for_user` / `waiting_for_confirmation` 与其 `CLARIFICATION_REQUIRED` / `CONFIRMATION_REQUIRED` | 丢弃这条待办，返回库里的权威状态 |
| 回答 / 拒绝确认 | `_active_task()` 读到的状态 | 原状态与 `SLOTS_UPDATED` / `CONFIRMATION_DECLINED` 一起 | 抛 `TaskConflict`，让调用方刷新后重试 |
| 取消 | `_owned()` 读到的状态 | `cancelled` + `CANCELLED` 事件 | 抛 `TaskConflict`，让调用方刷新后重试 |

仍在即时写的只剩纯过程事件（`STEP_STARTED`、`STEP_COMPLETED`），保证 SSE 不延迟：
它们既不改变任务形态，也不代表任何一个"在等谁"的承诺，输了也不欠谁什么。
凡是决定"任务现在等谁"的写入——终态、暂停态、字段更新——都进条件写。
成功路径不在这里单独落盘——`task.result` 一直攒到流程走完，与 `COMPLETED` 一起
提交，同样受 CAS 保护。

条件写输掉不是"写失败"，而是"另一个写者先赢了"。执行侧绝不拿旧快照重试，
取消侧也绝不反过来把完成覆盖掉；两边都交回库里的权威状态。这与步骤租约
"占用状态不明即拒绝"、取消信号"读不出来按已取消收口"是同一个口径：
**状态不明时不再产生新动作**。

### 暂停也是承诺，不只是中间态

暂停看起来只是"过程中间态"，所以最初被留在了条件写之外（三条分支用无条件
`save`，两条命令用 `save` + 独立 `emit`）。这个判断是错的：暂停态正是**对用户
的一份承诺**——"我在等你补充字段""我在等你确认这一次提交"。当这份承诺与取消
并发时，旧写法会留下两类残局：

- 取消先写入 `cancelled`，随后暂停分支拿旧快照 `save` 把它盖回
  `waiting_for_user` / `waiting_for_confirmation`——界面上"已取消"自己变回待输入；
- 状态侥幸没被盖掉，但 `CLARIFICATION_REQUIRED` / `CONFIRMATION_REQUIRED` 已经
  独立写进了事件流，于是事件里挂着一条永不消解的待办，人照着它再填一遍，
  而任务早就结束了。

两条残局的根都是"状态与承诺事件不同生共死"，与第 16 节的迟到结果完全同构，
所以用同一把仲裁：暂停连同待办事件一起 `commit_guarded`，输掉就整批丢弃。

开局还要多一道判断。`advance()` 进入时先读库里的状态；若它**已经是终态**，
直接返回这份权威状态，不往下走：

- 传进来的 `task` 可能是上一条命令手里的旧快照，在它被处理到这里之前任务已经
  取消或完成；
- 这时候若照旧拿读到的状态当期望值，期望值恰好就是这个终态，条件写反而会
  **成功**——旧快照加"库里恰好没变"把终态推进一步，比无条件覆盖更难发现。

### 已验证

- 两种后端跑同一组契约：期望匹配时状态与事件一起落、顺序号连续；期望不符时
  `StaleTaskState`，状态与事件一条都不变；不带期望值时保持旧的无条件语义；
  任务不存在报 `TaskNotFoundError`。
- SQLite 两个连接指向同一文件：先写者赢，后写者整批丢弃且不留半条事件。
- 用"在第一次条件提交前抢先改写状态"的包装 store 精确复刻旧窗口：
  `succeeded` / `unconfirmed` / `failed` 三种结果下都保持 `cancelled`，
  且 `COMPLETED` / `OUTCOME_UNCONFIRMED` / `EXECUTION_FAILED` / `FAILED`
  一条都不出现。
- 取消输掉竞态时报 `TaskConflict`，状态维持 `COMPLETED`，无 `CANCELLED`
  事件；正常完成与正常取消的事件流不受影响。
- 暂停分支同样过条件写：在首次暂停提交前抢先取消，任务保持 `cancelled`，
  `CLARIFICATION_REQUIRED` / `CONFIRMATION_REQUIRED` 一条都不出现，
  且不会留下 `pending_fields` 让界面继续催输入。
- 回答与拒绝确认输掉竞态时报 `TaskConflict`，`SLOTS_UPDATED` /
  `CONFIRMATION_DECLINED` 不落盘；任务状态维持对手写下的值。
- 手里是旧快照时再调一次 `advance()`：不放行、不补事件，直接返回库里的
  `cancelled`（`STEP_STARTED` 与 `CLARIFICATION_REQUIRED` 各仍只有一条）。
- 改成条件写后事件顺序号仍连续，不重号不跳号（暂停 → 回答 → 待确认）。

### 仍然没承诺的事

- 只覆盖单库（同机多进程共享 SQLite 文件）；跨主机多实例需要把任务状态放进
  一个所有写者共用的外部存储，本机事务仲裁不了另一台机器。
- 锁等待超时（另一个连接持有写锁）按 `StaleTaskState` 收口：状态无法确认时
  放弃本次结果，而不是带着疑问继续写事件。

## 18. 取消对任意执行器生效（开始前那一道）

第 14 节把取消写成"执行侧看得见的信号"，但那个信号只有 `HarnessExecutor` 自己的
循环在读。装配切到 Octop 运行时或脚手架执行器时，`POST /api/tasks/{id}/cancel`
又退回纯状态翻转：任务显示已取消，正在排队或刚开始的那一步照样把模型调用与
工具调用跑完，包括用户刚说"不要了"的那一次提交。

### 能承诺什么，取决于谁在读信号

四道边界里，只有**第一道**（执行开始前）与执行器内部实现无关，任何执行器都能被
统一兜住。`harness/cancel_executor.py` 的 `CancellableExecutor` 就是这一道：

```python
from workpanda.harness.cancel_executor import CancellableExecutor

executor = CancellableExecutor(OctopHarnessExecutor(gateway), cancellations)
```

它做的只有三件事：把请求交给内层之前取一次令牌、已取消就直接给出
`unconfirmed` + `cancelled`（一步都不跑）、无论结论如何都在 `finally` 里释放登记项。
默认装配已把 Octop 分支与脚手架分支包进这层（harness 分支自带取消，不再重复包）。

| 执行器 | 开始前 | 执行中 |
|---|---|---|
| `HarnessExecutor` | 生效（自带） | 生效（下一轮模型 / 下一条工具 / 退避等待） |
| Octop 运行时 | 生效（包装器） | 不生效——工具循环不在本服务手里 |
| 脚手架执行器 | 生效（包装器） | 不生效（占位实现，单次同步返回） |
| 自定义执行器 | 生效（包装器） | 只在它自己读令牌时生效 |

### 拒绝时长得和 harness 一样

拒绝结果的 `output` 形状刻意与 `HarnessExecutor._refuse` 对齐：
`stop_reason=cancelled`、`verification.reason=cancelled`、`model_calls=0`、
`tool_calls=[]`。`FlowRunner` 把这两个字段抄进任务事件，审计归档按
`stop_reason` 记账——形状不一致会让"取消"在留痕里出现两种互不相认的写法，
事后核对时最难解释的恰恰是这种差异。

### 刻意不做的事

- **不打断内层**，也不因为它"期间收到过取消"改写结论。执行已经发生，它到底做了
  什么只有内层知道；替它改判等于伪造一条没人能核实的记录。取消与迟到结果的仲裁
  仍由第 17 节的条件写负责——谁先写谁生效，输的一边连事件一起丢。
- **不包装 `HarnessExecutor`**：两层会为同一 `(task_id, step_id)` 各取一次令牌，
  内层收尾时就把外层还要用的登记项释放了。它已经自带取消，直接用即可。
- **不解决跨进程**：共享信号仍由第 16 节的 `CancelSignalStore` 负责，包装器只是
  在令牌上多读一次，后端换成 SQLite 还是 Redis 与它无关。

### 已验证

- 未取消时原样转发，执行结束登记表清空（不随任务数增长）；内层抛异常同样清空；
- 暂存的取消（比执行更早到达）在进入包装器时立刻生效：内层一次都没被调用，
  结论 `unconfirmed` + `cancelled`，`stop_reason`/`verification` 与 harness 拒绝一致；
- 同一条暂存取消只拦住那一次：随后同一任务的又一次执行正常下发到内层；
- 执行期间被取消且内层自己读到了令牌：包装器原样报告内层结论，不改写；
- 默认装配（脚手架）与 `octop_enabled` 装配都被包进这一层，内层执行器类型不变。
## 19. 工具线程台账与容量闸门

第 12 节第一条限制一直没被真正面对过：工具超时的实现是"独立线程 + 放弃等待"，
**线程本身收不回来**，它会继续跑到自然结束。只关心返回值的话，进程里究竟堆了
多少个卡住的线程就没人知道——真实长任务里，内存与句柄正是这样被悄悄吃光的。

### 记账：把"还在跑"和"已经放弃"分开

`ToolRegistry` 持有一份进程内的线程台账（`RunningToolThreads`），每次起线程前
登记、线程结束时自己摘掉：

| 状态 | 含义 | 谁写 |
|---|---|---|
| `live` | 线程还活着（含正常执行中的） | 起线程前登记，线程 `finally` 里摘除 |
| `stuck` | 已超时、主线程已放弃，但线程还没退出 | 主线程超时后 `abandon` |

两个细节值得写下来：

- **超时不摘账。** 超时只是"我不等了"，线程还在，账上就该还记着它。等它真的
  结束（哪怕是十分钟后）才由线程自己摘掉。反过来"超时就当它结束了"会让闸门形同虚设；
- **先结束、后放弃也必须无害。** 线程可能抢在主线程 `abandon` 之前跑完并摘账；
  此时 `abandon` 找不到那条记录，什么都不做——不能凭空多出一个"卡住"。

### 闸门：触顶时拒绝发起新调用，而不是继续堆线程

`stuck` 达到 `WORKPANDA_HARNESS_MAX_STUCK_THREADS`（默认 16）后，新的工具调用
直接记 `BLOCKED` + `tool_capacity`，**确定没执行**（不入副作用账本）：

- 拒绝发生在起线程之前，所以它和"未授权""参数非法"同属一类，不是"结果未知"；
- 拒绝**占掉这次尝试的预留**：否则模型对着一个被闸门挡住的工具可以无限重试，
  `max_tool_calls` 也就挡不住了；
- 上限设 0 表示关闭闸门，行为与加入本机制前一致——默认开着，因为"忘了设"远比
  "刻意关掉"常见。

这是刻意的 fail-closed：进程里已经堆了十几条收不回来的线程，继续起新的只会让
情况更糟，而重启与排查需要人来做。与租约"占用不明即拒绝"、取消信号"读不出来按
已取消收口"是同一个口径——**状态不明时不再产生新动作**。

### 看得到才算数

`GET /api/ops/tool-runtime` 返回 `{"available", "live", "stuck", "max_stuck"}`：

- 未启用 harness 时 `available=false`：没有工具线程，就不要编一排 0 出来；
- `stuck` 持续上涨说明某个工具真的卡住了，处理办法是修工具或重启进程，
  不是把上限调大。

### 已验证

- 正常结束的调用不留账（`live`/`stuck` 归零）；
- 超时调用计入 `stuck` 且保持 `live`，线程真正结束后两项都归零；
- `stuck` 触顶后新调用被 `BLOCKED` + `tool_capacity`，内层工具一次都没被调用，
  内容里带当前卡住数与上限；
- 触顶之后放行（线程结束）能立刻恢复执行；
- 上限为 0 时不拒绝；线程先结束、主线程后 `abandon` 不会凭空多出卡住的记录；
- 拒绝记录保留声明的风险等级，并确实占掉一次尝试（下一次会先撞 `tool_call_limit`）。
