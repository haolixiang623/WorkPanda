# Octop 集成说明

> 范围：说明 WorkPanda 如何接入 Octop-Harness 运行时，以及 Octop 控制台与 WorkPanda 的职责划分。
> 现状：运行时适配已实现并通过 SDK 构造验证；控制台对接只完成服务端只读接口，**插件尚未实现**。

## 1. 相关上游项目

| 项目 | 仓库 | 作用 |
|---|---|---|
| Octop-Harness | https://github.com/TencentCloud/octop-harness | 可嵌入的 Agent 运行时（`octop-harness`，MIT） |
| Octop | https://github.com/TencentCloud/Octop | 自托管多用户平台，含 Web 控制台、IM 网关、定时任务 |

WorkPanda 只依赖 **Octop-Harness SDK**，不整体部署 Octop 产品。控制台由对方提供，我们不再自建运维界面。

## 2. 职责划分

| 能力 | 归属 | 说明 |
|---|---|---|
| 业务流程、步骤顺序、工具白名单 | WorkPanda | 服务端固定配置，不下发客户端 |
| 任务状态、事件、确认凭证 | WorkPanda | Task Store 是唯一权威 |
| 群众鉴权、会话、限流 | WorkPanda | 匿名签名会话，后续替换为真实身份 |
| 模型推理、工具调用、会话检查点 | Octop-Harness | 只执行单个受控步骤 |
| 运维查看、运营排障界面 | Octop 控制台 | 只读消费 WorkPanda 的运维接口 |

关键约束：**控制台不写业务状态**。流程推进、群众确认和副作用只能通过群众侧接口完成，避免出现"控制台显示成功、业务侧仍待确认"的双写冲突。

## 3. 运行时接入

### 3.1 组件

- `OctopHarnessGateway`：封装 SDK。SDK 是异步 API，适配器用独立事件循环线程承载，避免阻塞 Web 事件循环。
- `OctopHarnessExecutor`：实现应用层 `Executor` 协议，把一次步骤执行转成一次受控调用。

### 3.2 结果映射

| 上游情况 | 映射结果 | 理由 |
|---|---|---|
| 正常返回 | `succeeded` | 有结构化输出 |
| 超时 | `unconfirmed` | 可能已产生副作用，不得当失败重试 |
| 运行时异常 | `unconfirmed` | 无法判定是否执行 |
| SDK 未安装 / 缺少凭据 | `failed` | 明确是配置问题，未接触外部系统 |

映射原则：**只有确定成功才报成功；不确定一律"待确认"并转人工**。

### 3.3 提示词边界

`default_prompt` 只放当前步骤的流程标识、步骤、工具名和该步骤参数，并显式要求不得改变流程或调用其他工具。不把完整流程定义、其他用户数据或历史会话交给运行时。

### 3.4 启用方式

```bash
python -m pip install -e '.[octop]'
export WORKPANDA_OCTOP_ENABLED=true
export WORKPANDA_OCTOP_API_KEY=...            # 必须
export WORKPANDA_OCTOP_MODEL=gpt-4o-mini
export WORKPANDA_OCTOP_PROVIDER=openai        # openai / anthropic
export WORKPANDA_OCTOP_BASE_URL=...           # 兼容接口时必填
export WORKPANDA_OCTOP_WORKSPACE=...          # 必须是绝对路径或可解析路径
```

未启用时执行器仍是 `ScriptedExecutor`，仅用于骨架自检。

## 4. 控制台接入

### 4.1 服务端提供的只读接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/ops/tasks?limit=&status=` | 按创建时间倒序列出任务 |
| GET | `/api/ops/tasks/{task_id}` | 任务详情 |
| GET | `/api/ops/tasks/{task_id}/events?after=` | 事件流（含澄清、确认、执行结果、转人工） |

鉴权：请求头 `X-WorkPanda-Ops-Token` 必须等于 `WORKPANDA_OPS_TOKEN`。

- 未配置该变量时，整个 `/api/ops` 返回 404，不暴露内部接口存在；
- 凭据错误返回 401；
- 群众会话令牌**不能**访问运维接口。

### 4.2 控制台侧的做法

Octop 控制台支持通过插件注入自定义 UI 与 Hook/Tool/Skill。推荐：

1. 写一个只读 Octop 插件，调用上述接口渲染任务列表、状态分布和失败事件；
2. 插件只展示，不提供"改状态""重试执行"按钮；
3. 需要人工处置时，跳转到人工工单或联系渠道，而不是直接修改 Task Store；
4. 插件凭据单独下发，不复用模型 API Key。

这部分**尚未实现**：插件属于 Octop 一侧的交付物，需要先有可访问的 Octop 实例。

## 5. 已验证与未验证

已验证：

- `octop-harness 1.0.8` 可安装；
- 适配器能按其真实签名构造 `HarnessAgent`（`tests/test_octop_runtime.py::test_real_sdk_builds_harness_agent`）；
- 三态映射、缺少凭据、未安装 SDK 的降级行为有单元测试覆盖。

未验证：

- **真实模型调用**：没有可用 API Key，未跑通端到端推理；
- **Octop 控制台插件**：未实现，也未在真实 Octop 实例上验证；
- **并发与多实例**：运行时每个进程各建一个 Agent，多实例的会话与限流一致性未验证；
- **浏览器类工具**：`octop-browser` 随 SDK 安装，但 WorkPanda 尚未启用，也未做域白名单与用户隔离验证。

## 6. 风险与后续

1. 模型输出不可信：运行时返回内容仍需按步骤校验，不能直接当业务结果；
2. 工具越权：启用浏览器或 MCP 工具前，必须先做工具白名单与目标域名限制；
3. 成本与时延：单步骤超时独立配置，避免长任务拖垮请求；
4. 数据边界：只传当前步骤所需字段，敏感字段需脱敏后再进入提示词；
5. 多实例：Agent 与限流状态均为进程内，横向扩容前需外部化。
