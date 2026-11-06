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
├── tools.py          工具协议、注册表、调用授权、参数校验
├── budget.py         步数、工具调用次数、耗时三重预算
├── loop.py           有界执行循环（模型 ↔ 工具）
├── clients.py        模型端口 + 脚本化客户端（测试用）
├── openai_client.py  OpenAI 兼容客户端，支持 function calling
├── builtin.py        内置受控工具
└── executor.py       接到应用层 Executor 端口
```

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

## 3. 六条不变式

1. **最小可见**：模型只能看到本步骤被授权的工具子集，看不到全局注册表。
2. **三道闸**：白名单 → 参数校验 → 执行并留痕。未授权/未注册/参数不合法的调用一律 BLOCKED，绝不"尽力执行"。
3. **有界**：步数、工具调用次数、总耗时三重上限。超界一律"未确认"，因为此刻可能已调用过有副作用的工具。
4. **可追溯**：每次模型调用与工具调用都写入 trace，事后必须能完整重放发生了什么。
5. **不允许口头成功**：本步骤有可用工具，模型却一次都没成功调用却声称 `succeeded`，降级为 `unconfirmed`（`unverified_success`）。
6. **不认识业务**：只回答"这一步要做什么"，不决定"该办什么事"。业务流程、状态、确认凭证仍在应用层与领域层。

## 4. 三态映射

| 情况 | 结果 | 错误码 |
|---|---|---|
| 契约 `succeeded` + 有 `evidence` + 有成功工具调用（或本步骤无工具） | `succeeded` | — |
| 契约 `succeeded` 但无 `evidence` | `unconfirmed` | `missing_evidence` |
| 契约 `succeeded` 但有工具却零成功调用 | `unconfirmed` | `unverified_success` |
| 契约 `failed` | `failed` | `failed` |
| 不是合法 JSON | `unconfirmed` | `unparsable_output` |
| 步数 / 调用次数 / 耗时超限 | `unconfirmed` | `budget_max_steps` / `budget_max_tool_calls` / `budget_deadline` |
| 模型超时 | `unconfirmed` | `model_timeout` |
| 模型服务错误（4xx/5xx、传输失败） | `unconfirmed` | `http_xxx` / `transport_error` |
| 连续空回复 | `unconfirmed` | `empty_model_response` |

原则：**只有确定成功才报成功；不确定一律"待确认"并转人工。**

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
- [ ] 有副作用的工具设 `idempotent=False`，并在流程里配确认步骤
- [ ] 在 `default_registry` 中注册；默认不注册 = 默认不可用
- [ ] 新增工具后补一条"未授权时不会被调用"的测试

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

## 6. 配置

| 变量 | 默认值 | 说明 |
|---|---|---|
| `WORKPANDA_HARNESS_ENABLED` | `false` | 启用通用 harness 执行器 |
| `WORKPANDA_MODEL_BASE_URL` | 空 | OpenAI 兼容接口地址，如 `http://host/v1` |
| `WORKPANDA_MODEL_API_KEY` | 空 | 模型凭据 |
| `WORKPANDA_MODEL_NAME` | `gpt-4o-mini` | 模型标识 |
| `WORKPANDA_MODEL_TIMEOUT_SECONDS` | `60` | 单次模型调用超时 |
| `WORKPANDA_MODEL_TRUST_ENV` | `true` | 是否沿用系统代理变量；内网地址被代理拦截时置 `false` |
| `WORKPANDA_HARNESS_MAX_STEPS` | `6` | 单步骤最多几轮模型调用 |
| `WORKPANDA_HARNESS_MAX_TOOL_CALLS` | `8` | 单步骤最多几次工具调用 |
| `WORKPANDA_HARNESS_DEADLINE_SECONDS` | `45` | 单步骤总耗时上限 |
| `WORKPANDA_HARNESS_ALLOWED_HOSTS` | 空 | 逗号分隔的 http 工具域名白名单 |

`HARNESS_ENABLED` 为 true 且模型三项配置齐全时才启用；否则回落到 Octop 运行时或脚手架执行器。

## 7. 验证

```bash
pytest tests/test_harness.py tests/test_openai_client.py
python scripts/smoke_harness.py     # 真实模型端到端
```

`smoke_harness.py` 做三个硬断言：

1. 声明了 `sys_clock` 的步骤 → 模型必须真的调用它，成功且 `tool_evidence` 可追溯；
2. 工具未注册的步骤 → 只能如实报告，不能报完成；
3. 失败必须留痕（模型原文进入任务事件）。

## 8. 已验证与未验证

已验证：

- 白名单拒绝、参数校验、工具异常、风险归并均有单元测试；
- 预算三类耗尽、模型超时、模型错误、连续空回复均映射为未确认；
- trace 覆盖每次模型与工具调用；
- OpenAI 兼容客户端的线上格式（tools 声明、tool_calls 回传、畸形参数、4xx/超时）；
- 真实模型（内网 `deepseek-v4-1-flash`）下工具真实执行：`model_calls=2`，
  `evidence` 即 `sys_clock` 返回的时间戳。

未验证：

- 多工具串联与并行调用；
- 真实业务系统工具（尚未接入任何外部 API）；
- 长耗时工具在同步执行器下的阻塞影响；
- 多实例部署下的预算与限流一致性。

## 9. 已知限制

- 工具是同步执行，长任务会占用工作线程，高并发需改线程池或异步工具；
- 预算按调用次数与耗时计，**不按 token 计**，成本控制需要另行实现；
- 无 checkpoint：一步失败即终止，不能从中断处恢复（Agent 会话恢复未做）；
- 事件总线与预算状态都在单进程内，横向扩容前需外部化；
- trace 目前随任务事件存储，缺少独立的检索与聚合能力。
