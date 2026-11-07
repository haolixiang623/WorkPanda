# Octop 集成说明

> 范围：说明 WorkPanda 如何接入 Octop-Harness 运行时，以及 Octop 控制台与 WorkPanda 的职责划分。
> 现状：运行时适配已实现，真实模型端到端已跑通（含结构化结果契约）；只读控制台插件已实现（`plugins/workpanda-ops/`），并在本机 Octop 实例上完成安装、加载、模型接入、真实聊天工具调用与 Dashboard 浏览器内渲染的全链路走查（见第 4 节）。

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

### 3.2 结构化结果契约

**"模型有回复"不等于"业务成功"**。早期实现把任何非空回复都当成成功，在内网模型上实测时出现过这样的情形：模型明确回复"当前运行时没有注册名为 `demo.echo` 的工具，无法执行"，任务却仍被标记为 `completed`。这是本项目最危险的一类缺陷——群众看到"办理成功"，实际什么都没发生。

因此执行步骤强制采用结构化契约。提示词要求模型只返回一个 JSON 对象：

```json
{"outcome": "succeeded|failed|unconfirmed", "summary": "做了什么", "evidence": "可核验依据"}
```

`parse_contract` 的判定规则：

| 模型输出 | 映射结果 | 错误码 |
|---|---|---|
| `succeeded` 且有 `evidence` | `succeeded` | — |
| `succeeded` 但 `evidence` 为空 | `unconfirmed` | `missing_evidence` |
| `failed` | `failed` | `failed` |
| 其他取值 / `unconfirmed` | `unconfirmed` | 原值或 `unspecified_outcome` |
| 不是合法 JSON（如白话拒绝） | `unconfirmed` | `unparsable_output` |

允许 ```json 代码块围栏。**没有例外路径可以绕过 evidence 检查**。

### 3.3 结果映射

| 上游情况 | 映射结果 | 理由 |
|---|---|---|
| 契约为 `succeeded` 且有依据 | `succeeded` | 有可核验输出 |
| 契约缺失依据 / 无法解析 / 自述无法完成 | `unconfirmed` | 不得默认成功 |
| 超时 | `unconfirmed` | 可能已产生副作用，不得当失败重试 |
| 运行时异常 | `unconfirmed` | 无法判定是否执行 |
| SDK 未安装 / 缺少凭据 | `failed` | 明确是配置问题，未接触外部系统 |

映射原则：**只有确定成功才报成功；不确定一律"待确认"并转人工**。

失败与未确认同样要留痕：`ExecutionResult.output` 始终携带模型原文与契约解析结果，`FlowRunner` 把它们写入 `execution_failed` / `outcome_unconfirmed` 事件的 `model_text`、`verification` 字段。否则事后无法复盘，也无法向群众解释为什么转人工。

### 3.4 提示词边界

`default_prompt` 只放当前步骤标识、该步骤的执行要求（来自流程定义的 `instruction`）和参数，并附带结果契约。不把完整流程定义、其他用户数据或历史会话交给运行时。

一个容易踩的坑：步骤的 `tool` 是**服务端**为该步骤选定的能力标识（用于记录、审计、决定由哪个执行器执行），不是要求模型调用的工具。早期提示词写成"任务步骤：x（工具 model.echo）"，模型会去找同名工具，找不到就误报失败。现在提示词显式说明这一点，模型只按执行要求完成并如实报告。

步骤意图由流程定义的 `instruction` 决定，**模型不得自行改写或猜测该做什么**。

### 3.5 端到端冒烟

`scripts/smoke_octop.py` 用真实模型跑两个场景并做硬断言：

1. 可完成的步骤 → 必须 `completed` 且带 `evidence`；
2. 模型无法真正完成的步骤 → 必须落到 `outcome_unconfirmed` / `handoff_required`，**绝不能是 `completed`**，且事件里必须留下模型原文。

```bash
python -m pip install -e '.[octop]'
python scripts/smoke_octop.py    # 依赖 .env 中的 Octop 配置
```

### 3.6 启用方式

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
| GET | `/api/ops/tasks?limit=&status=&flow_id=&since=&until=&cursor=` | 按创建时间倒序列出任务 |
| GET | `/api/ops/tasks/{task_id}` | 任务详情 |
| GET | `/api/ops/tasks/{task_id}/events?after=&type=&since=&until=&limit=&cursor=` | 事件流（含澄清、确认、执行结果、转人工） |
| GET | `/api/ops/traces?task_id=&limit=&outcome=&stop_reason=&risk=&since=&until=&cursor=` | 执行留痕 |
| GET | `/api/ops/traces/summary` | 留痕聚合指标（未确认占比、被拦/超时调用、token 等） |
| GET | `/api/ops/audit?task_id=&kind=&since=&until=&limit=&cursor=` | 审计归档条目 |
| GET | `/api/ops/tool-runtime` | 工具线程台账与容量闸门（第十一轮新增） |

所有列表接口的响应体都是**数组**，翻页信息放在 `X-Next-Cursor` 响应头：把这个值
原样作为下一页的 `cursor` 参数回传即可；头不存在就是到头了。游标不透明且带端点
scope，跨端点复用会得到 `400 invalid_cursor`，不会"看起来正常地"返回一页错数据。

鉴权：请求头 `X-WorkPanda-Ops-Token` 必须是某个运维主体的凭据，主体与角色由
`WORKPANDA_OPS_TOKENS`（`主体:角色:密钥`）或单主体旧配置 `WORKPANDA_OPS_TOKEN` 定义。

- 未配置任何凭据时，整个 `/api/ops` 返回 404，不暴露内部接口存在；
- 凭据错误返回 401；
- 凭据有效但角色不够返回 403：`viewer` 只能读任务列表与留痕聚合，任务详情、
  事件流、留痕明细、审计归档、工具台账要求 `admin`；
- 群众会话令牌**不能**访问运维接口，运维凭据也不能当会话令牌用。

角色划分、凭据轮换与 TLS/回退见 [OPS-ACCESS-CONTROL.md](OPS-ACCESS-CONTROL.md)。
控制台插件所需的是 `admin` 角色凭据（它要读留痕与审计明细）。

### 4.2 只读插件（已实现）

插件源码在 `plugins/workpanda-ops/`，遵循 Octop `docs/plugin-package.md`：

- `plugin.yaml`：`kind: tool`，`ui.entry: ui/dist/index.js`、`ui.manifest: ui/dist/manifest.json`；
- `main.py`：`setup(ctx)` 注册 4 个读工具（overview / tasks / traces / audit），返回
  `octop_ui` envelope；零第三方依赖（仅标准库，安装时不会触发 pip 拉包）；
- `ui/dist/index.js`：自包含 ESM，`setup(host)` 注册 `workpanda_ops` 渲染器，
  用宿主注入的 `window.__OCTOP_REACT__.createElement` 渲染指标卡片与只读表格。

规范要点（官方 `plugin-package.md`）：`kind` 是 `tool`/`skill`/`hook`/`channel`
**四选一**，`setup` 只能注册与该 kind 对应的方法；`ui` 是可选附加项，并非
「一个插件同时注入 UI + Hook + Tool + Skill」。UI 必须是预构建 ESM，
安装时不跑 `npm install`，不能有相对 import。

只读边界：

1. 只发 `GET`，URL 路径是代码内常量，不接受调用方传入地址；
2. 不注册改状态、重试执行、取消任务等任何写入口；
3. 只透传白名单字段并截断字符串，不展示会话标识、失败原文、槽位内容；
4. 凭据走 `ops_token` 插件配置或 `WORKPANDA_OPS_TOKEN` 环境变量，
   单独下发、不复用模型 API Key、不硬编码、不回显；
5. 需要人工处置时走既定转人工渠道，不直接修改 Task Store。

### 4.3 本机试点部署（持久化）

试点不再跑在 `/tmp`。`deploy/workpanda_deploy.py`（仅标准库）把两个服务装成
launchd 托管的常驻进程，全部状态落在 `~/Library/Application Support/WorkPanda`：

| 路径 | 内容 |
|---|---|
| `api-venv/` | WorkPanda 自己的 venv（`pip install <仓库>`，与工作树解耦） |
| `octop-venv/` | Octop 1.0.2 |
| `octop-home/.octop/` | Octop 数据目录：数据库、插件、模型与 agent 配置 |
| `data/workpanda.db` | 任务库（SQLite） |
| `logs/` | 两个进程的标准输出与错误 |
| `api.env` | 非敏感配置（0600） |
| `bin/run-api.sh`、`bin/run-octop.sh` | 启动包装脚本（0700） |

- 端口固定：Octop **8090**（本机 8088 已被 Apache Tomcat 占用）、WorkPanda
  **8010**（8000 被别的 FastAPI 服务占用），都只监听 `127.0.0.1`；
- launchd 单元 `~/Library/LaunchAgents/com.workpanda.{api,octop}.plist`，
  `RunAtLoad` + `KeepAlive`：机器重启后自动拉起，进程异常退出也会被拉起；
- 凭据在登录钥匙串，service `workpanda-deploy`，账户名为环境变量名：
  `WORKPANDA_OPS_TOKEN`、`WORKPANDA_SESSION_SECRET`、`WORKPANDA_MODEL_API_KEY`、
  `WORKPANDA_OCTOP_API_KEY`、`OCTOP_ADMIN_PASSWORD`。包装脚本启动时读取并注入，
  plist 与 `api.env` 里没有明文（`api.env` 只接受白名单内的非敏感键）；
- 安装：`octop plugin install <插件目录> --force`（1.0.2 的 `plugin install`
  只接受目录或 URL，**不接受本地 zip 路径**），随后 `octop plugin reload` 并重启
  进程使状态生效。CLI `octop plugin list` 的 `[not loaded]` 是离线读 DB 的静态
  字段，应以 `/api/plugins` 的 `loaded` / `registration_error` 为准。

两个已经踩过、并且都写了回归测试的坑：

1. **先去钥匙串、再改 HOME。** `security` 按 `$HOME/Library/Keychains` 找登录
   钥匙串。包装脚本若先把 `HOME` 指到 `octop-home` 再取凭据，就会「找不到条目」
   并把运维令牌静默丢掉 —— 服务看起来一切正常，插件工具却全部报缺凭据。
2. **迁移要改写 agent 里的旧路径。** 工作区绝对路径存在
   `agents.config_json` 里；只搬目录不改库，agent 仍指向 `/tmp/...`，临时目录
   一清就找不到工作区。`install --from-octop-home` 会顺手把旧 HOME 前缀换成
   新的持久目录。

已验证（2026-11-07，本机）：`deploy/workpanda_deploy.py install` 从
`/tmp/wp-octop-home` 迁移数据后，两个 agent 均为 `running`；
WorkPanda `/healthz` 与 `/api/ops/tasks` 都是 `200`（后者说明钥匙串里的运维
令牌确实注入到了进程）；Octop `/api/health` 返回 `{"ok":true,...}`；匿名访问
`/api/plugins` 仍是 `401`（没有为排障把控制台放开）；`bootout` + `bootstrap`
重启一轮后两个服务自动恢复、探针全绿；在 Octop 自身 venv 里 4 个工具真实取到
WorkPanda 数据（overview / tasks / traces / audit 均返回 `octop_ui` envelope）；
`api.env`、两个 plist、两个包装脚本逐字比对均无明文凭据，`git grep` 亦无命中。

仍需登录后人工确认的一次：控制台插件页的「已加载」标签与 4 个工具的
`enabled` 开关（`/api/plugins`、`/api/plugins/agents/{id}/tools` 都要管理员
会话，自动化只覆盖到离线 reload + 工具取数这一层）。

### 4.4 控制台全链路走查（2026-11-07）

用 Playwright + 系统 Chrome 真实登录 `http://127.0.0.1:8090`（滑块验证），
逐项确认「控制台 → 模型 → 专家 → 插件 → 工具 → WorkPanda 数据 → UI 卡片」整条链路：

| 环节 | 走查结果 |
|---|---|
| 插件管理页 `/admin/plugins` | `WorkPanda 运维只读`、`tool v0.1.0`、**已加载**、启用开关打开 |
| 模型页 `/admin/models` | `dashscope` Provider 已启用，base_url 为百炼兼容端点，activity model 为 `deepseek-v4.1-flash-1017` |
| 专家页 `/experts` | `WorkPanda 运维助手`（`agent_id=DZMF8E`）可见 |
| 工具挂载 | `/api/plugins/agents/DZMF8E/tools` 返回 4 个 `workpanda_ops_*` 工具，`enabled=true`（插件工具默认开启） |
| 真实聊天 | WebSocket `user_turn` 触发模型调用工具，收到 `tool_call_chunk` → `tool_result`（含 `octop_ui` envelope）→ `done`，无 error |
| 浏览器渲染 | 4 个工具各渲染一张 `WorkPanda 运维视图 / 只读` 卡片：overview 为指标网格；tasks、traces 为带中文表头的表格（任务 ID / 流程 / 状态、留痕 ID / 步骤 / 结果 …）；audit 无数据时显示「暂无记录」；`pageerror` 为空 |

OpenAPI 面探测：`/api/openapi.json` 共 416 个路径、38 个可无参 GET 的模块，
除 `/api/ollama-models` 因 Ollama 功能开关未启用返回 503 外，无 5xx；
少数 4xx 是缺必需参数或 MCP session 协议要求，不是服务异常。
`enable_api_docs` 已在诊断后关闭，现配置为 `false`；`bind_host` 仍是
`127.0.0.1`、`tls.enabled` 仍是 `false`（持久化不改变对外暴露面）。

未验证：Octop 控制台自身**账号**之间的权限隔离、Ollama provider 启用，以及生产
部署形态（TLS 与绑定门禁已具备，但真实证书与对外绑定未做演练，
见 [OPS-ACCESS-CONTROL.md](OPS-ACCESS-CONTROL.md)）。
实例已迁出 `/tmp`、改由 launchd 托管（第 4.3 节），但仍是本机回环形态：
TLS、对外绑定与多客户端隔离都没有在试点里演练过。

## 5. 已验证与未验证

已验证：

- `octop-harness 1.0.8` 可安装；
- 适配器能按其真实签名构造 `HarnessAgent`（`tests/test_octop_runtime.py::test_real_sdk_builds_harness_agent`）；
- 三态映射、缺少凭据、未安装 SDK 的降级行为有单元测试覆盖；
- **真实模型端到端已跑通**：内网 OpenAI 兼容接口（`deepseek-v4-1-flash`）下，可完成步骤得到 `completed` + `evidence`；模型无法执行的步骤得到 `handoff_required` + `failed`，且事件中保留了模型原文，不再出现虚假成功（`scripts/smoke_octop.py`）。

未验证：

- **Octop 控制台插件**：已实现并在本机 Octop 1.0.2 实例上完成安装、加载、工具取数、真实聊天调用与浏览器渲染走查（第 4.3、4.4 节）；WorkPanda 侧的运维角色隔离与凭据轮换已实现（第 4.1 节），控制台账号之间的隔离与生产部署形态仍未验证；
- **并发与多实例**：运行时每个进程各建一个 Agent，多实例的会话与限流一致性未验证；
- **浏览器类工具**：`octop-browser` 随 SDK 安装，但 WorkPanda 尚未启用，也未做域白名单与用户隔离验证；
- **真实业务工具**：当前步骤由模型按 `instruction` 直接完成，尚未接入任何外部业务系统；接入时需补工具白名单与幂等设计。

## 6. 风险与后续

1. 模型输出不可信：结构化契约已经杜绝"有回复即成功"，但契约只保证模型自述与依据一致，**业务正确性仍需按步骤做服务端校验**；
2. 工具越权：启用浏览器或 MCP 工具前，必须先做工具白名单与目标域名限制；
3. 成本与时延：单步骤超时独立配置，避免长任务拖垮请求；
4. 数据边界：只传当前步骤所需字段，敏感字段需脱敏后再进入提示词；
5. 多实例：Agent 与限流状态均为进程内，横向扩容前需外部化。
