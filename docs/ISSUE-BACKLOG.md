# Issue 清单

> 来源：对话式原型评审 + 两次代码走查（流程定义/SOP 能力、群众页与 harness 的边界）。
> **状态：已审核（2026-11-09 19:00 UTC+8），尚未提交到 Issue 跟踪器。** 提交时按
> 批次逐条创建，正文用 `.github/ISSUE_TEMPLATE/feature.yml` / `bug.yml` 的字段组织。
> 对话式那 4 条的完整正文已在 [`PROPOSAL-CHAT-UI.md`](PROPOSAL-CHAT-UI.md)，此处只做索引。
>
> **A1 / A2 / C1 已在各自 PR 中处理完毕，见文末第 6 节。** 提交前先核一遍，避免把
> 已经修掉的缺陷重复提成 issue。

## 0. 总览

| # | 标题 | 类型 | 风险 | 依赖 | 批次 |
|---|---|---|---|---|---|
| A1 | 办结屏把执行器内部元数据当结果显示 | 缺陷 | 中 | — | 1 |
| A2 | 管理侧草稿表单漏 `field_labels` / `intent_rules` | 缺陷 | 中 | — | 1 |
| A3 | 自检流程每次启动重新发布，覆盖人工停用 | 缺陷 | 低 | — | 1 |
| B1 | 服务端下发对话元数据：`flow_title` + 敏感标记 | 功能 | 中→高 | — | 2 |
| B2 | `transcriptOf()`：由 `TaskView` 派生对话记录 | 功能 | 中 | B1 | 2 |
| B3 | 群众页改为对话式：消息流 + 常驻输入框 | 功能 | 中 | B2 | 2 |
| C1 | 让流程里声明的 `tool` 在 harness 下真的可用 | 功能 | 中 | — | 3 |
| C2 | SSE 跨实例漏提醒，页面无轮询兜底 | 缺陷 | 中 | — | 3 |
| C3 | `confirm` 长请求占工作线程，缺容量基线 | 功能 | 中 | — | 3 |
| D1 | 输入框附件入口（➕） | 功能 | **高** | 需决策 | 待定 |

批次含义：**1 = 纯修复，可并行**；**2 = 串行三连，一条主线的三个切片**；**3 = 运维/容量**；
**待定 = 需要业务或安全确认后才能排期**。

## 1. 提交策略（很重要，别踩自动 worker）

仓库有 `worker1` / `worker2` 每小时自动化，按 `scripts/issue_claim.py` 认领 Issue。它的门禁是
「**`enhancement`** 标签 + 无 assignee + 无 `issue/<n>-*` 远端分支 + 无有效 claim」。

- 用模板提交时默认标签是 `type:feature` / `type:bug`，**不是 `enhancement`**，所以不会被自动认领；
- **不要**手工补 `enhancement`，否则除了 D1（高风险）之外都会在下一小时被领走；
- 需要人决策的条目（B1 的敏感词口径、D1 全文）用 `question` 标签，该标签按协议不自动领取；
- 提交后立刻在本机 `python scripts/issue_claim.py list` 复核一次，确认没有出现在可认领列表里。

## 2. 逐条说明

### A1 · `[缺陷] 办结屏把执行器内部元数据当成结果显示`

- **风险**：中 ｜ **依赖**：无 ｜ **label**：`type:bug`
- **证据**：`harness/executor.py:554` 的 `_output()` 把 `trace_id / tool / step_id / text /
  model_calls / tokens / duration_ms / stop_reason / risk` 一并放进 `output`；
  `domain/models.py:229` 原样塞进 `TaskView.result`；`web/src/state_view.ts:184` 的
  `resultRows()` 只过滤 `_` 前缀和嵌套值，其余标量全部印出，键名还不翻译。
- **现象**：真实走查里完成屏逐行显示 `trace_id=… tokens=34 stop_reason=end_turn`，
  而业务结果（受理编号）与内部字段混在一起，群众无法判断办成没办成。
- **验收要点**
  - [ ] `TaskView` 改下发 `result_summary: [{name,label,value}]`，群众侧不再下发 `result`；
  - [ ] 白名单由流程作者声明（`StepDefinition.result_fields` + `result_labels`），页面照单全收；
  - [ ] `resultRows()` 在没有白名单时返回空，**不得**退化成打印原始 `result`；
  - [ ] 回归用**真实信封形状**（含 `trace_id` 等 9 个键）断言页面不出现内部键；
  - [ ] 确认 `admin-ui` / 运维脚本是否依赖 `TaskView.result`（若依赖，收窄需同步）。
- **不做**：不做结果字段的可视化排版；不改执行器输出结构。

### A2 · `[缺陷] 管理侧草稿表单漏字段，建出来的流程不可识别`

- **风险**：中 ｜ **依赖**：无 ｜ **label**：`type:bug`
- **证据**：`admin-ui/src/main.ts` 的全部 `data-key` 共 12 个，**没有 `field_labels`、没有
  `intent_rules`**；而 `api/admin.py:166` 的 `FlowDraftRequest` 两个字段都收。
- **后果**：纯 UI 建出的流程（a）群众看到的是 `subject` 而不是「办理主题」；
  （b）没有识别规则 → 一句话永远命中不了 → 只能靠客户端显式传 `flow_id` 才能建任务。
  等于「界面上建好了，但办不了」。
- **验收要点**
  - [ ] 步骤行可编辑 `field_labels`（键值对，键限于该步 `required_fields`）；
  - [ ] 可编辑 `intent_rules`（`intent_id` / `keywords` / `slot_patterns`），保存前本地校验
        指向的是本版本（服务端 `flow_field_errors` 已强制，前端提前提示）；
  - [ ] 422 的字段级 `loc` 能标到对应输入框（现有机制沿用）；
  - [ ] 回归：用 UI 表单（或其提交体）建一条草稿，断言 `field_labels` 与 `intent_rules` 非空落地。
- **不做**：不做流程可视化编排（画布）；不做版本 diff。

### A3 · `[缺陷] 自检流程每次启动重新发布，覆盖人工停用`

- **风险**：低 ｜ **依赖**：无 ｜ **label**：`type:bug`
- **证据**：`config.py:230` 的 `enable_demo_flow` **默认 `True`**；`bootstrap.py:326`
  的 `seed_demo_flows(enabled=True)` 无条件 `add_version` + `publish(actor="bootstrap")`。
- **后果**：运维在管理侧停用 `demo.generic` 后，重启即被重新发布——「正式接入业务前必须
  关闭」这条要求实际上关不住。且指针上的 `published_by` 被改写成 `bootstrap`，人工署名丢失。
- **验收要点**
  - [ ] 已发布/停用决策存在时，播种**不覆盖**指针（仅补版本）；只在从未有过指针时发布；
  - [ ] 停用后重启，`flow_releases.published=False` 与 `disabled_by` 保持原样；
  - [ ] 回归：连续两次装配，第二次不改变 `updated_at` 与 `published_by`；
  - [ ] 顺带把默认值改为 `False`，并在启动日志里明确提示当前状态。
- **不做**：不动 `SEED_ACTOR` 语义（它仍是"装配期同步"，不是人名）。

### B1 · `[功能] 服务端下发对话元数据：flow_title + 字段敏感标记`

- **风险**：中→高 ｜ **依赖**：无 ｜ **label**：`type:feature` + `question`
- **详情**：见 `PROPOSAL-CHAT-UI.md` 的 Issue 1。
- **为什么绕不过去**：`Task` **不存 `input_text`**，开场说不出"您要办哪件事"；
  `StepDefinition` 没有敏感标记，页面无从打码（原型的打码靠内置事项自带 `mask`）。
- **需先定**：敏感字段漏声明时是"拒绝发布"还是"允许发布但强制全部打码"。

### B2 · `[功能] transcriptOf()：由 TaskView 派生对话记录`

- **风险**：中 ｜ **依赖**：B1 ｜ **label**：`type:feature`
- **详情**：见 `PROPOSAL-CHAT-UI.md` 的 Issue 2。
- **核心不变量**：消息流**只能**由 `TaskView` 派生，不累积本地数组；`id` 稳定；派生幂等。

### B3 · `[功能] 群众页改为对话式：消息流 + 常驻输入框`

- **风险**：中 ｜ **依赖**：B2 ｜ **label**：`type:feature`
- **详情**：见 `PROPOSAL-CHAT-UI.md` 的 Issue 3。
- **会撞的既有回归**：`tsc` 必须重编并提交 `dist/`；`console.` / `location.` / `localStorage`
  禁用；新增 `must("x")` 必须在 `index.html` 有对应 id；`/confirm`、`/answers` 在
  `main.ts` 中各只能出现一次。

### C1 · `[功能] 让流程里声明的 tool 在 harness 下真的可用`

- **风险**：中 ｜ **依赖**：无 ｜ **label**：`type:feature`
- **证据**：`harness/builtin.py:176` 的 `default_registry()` 只注册 `sys_clock`（外加可选的
  http fetch 与浏览器工具）；而唯一的流程 `demo_flows.py:90` 声明 `tool="model_echo"`。
  `harness/executor.py:534` 的 `_grant()` 会按注册表交集过滤 → `allowed=∅`，配合
  `require_tool_call=True`，该步骤在真实 harness 下拿不到任何工具。
- **后果**：当前所有流程**只能在 `ScriptedExecutor`（脚手架）下跑通**。接第一个真实事项时，
  "配好流程就能办"这条路第一步就断在这里。
- **验收要点**
  - [ ] 明确取向（二选一，需你定）：
        a) 把自检流程改用已注册的 `sys_clock`（最小改动，但自检不再回显参数）；
        b) 注册一个真正回显参数的只读工具（更贴近"演示一次完整办理"）；
  - [ ] 装配期校验：流程声明的 `tool` 不在注册表内时，**启动即报错**或明确告警，
        而不是等到执行时 grant 为空；
  - [ ] 回归：起 harness（可用假模型客户端）跑一遍自检流程，断言 `tool_calls` 非空且
        `outcome=succeeded`；未注册工具的场景断言被拒绝且原因可读。
- **不做**：不接任何真实业务系统 API；不做工具市场。

### C2 · `[缺陷] SSE 跨实例漏提醒，页面无轮询兜底`

- **风险**：中 ｜ **依赖**：无 ｜ **label**：`type:bug`
- **证据**：`bootstrap.py:556` 是 `broker = EventBroker()`，进程内对象；
  `infrastructure/event_broker.py` 自己的文档写明"只统计本进程"。
- **后果**：用户在实例 A 订阅、事件在实例 B 产生 → 页面收不到提醒；而 `web/src/main.ts`
  **没有任何轮询**，只有 15s 的 `keepalive` 空转（`config.py:65`）。最坏情况是页面停在旧状态，
  直到用户下一次操作。单机试点无感，横向扩容即失效。
- **验收要点**
  - [ ] 给出结论：接共享 broker（与 `RedisSharedState` 同一套装配）还是页面加退避轮询兜底；
  - [ ] 双实例场景下，事件在非订阅实例产生，页面在 N 秒内看到新状态；
  - [ ] 兜底路径不得绕过"状态只来自 GET"的纪律（轮询只能是 GET）；
  - [ ] 回归：假 broker 丢一次推送，断言页面仍能追上（不能只测推送正常的路径）。

### C3 · `[功能] confirm 长请求占工作线程，缺容量基线`

- **风险**：中 ｜ **依赖**：无 ｜ **label**：`type:feature`
- **证据**：`api/routes.py:89` 用 `asyncio.to_thread` 调 `confirm`，而 `confirm` 同步走到
  `executor.execute()`；`config.py:146` deadline 45s、`config.py:160` 并发卡在
  `harness_max_stuck_threads=16`。
- **后果**：单实例同时最多 16 个"确认后执行"在进行，第 17 个排队；群众侧表现为转圈很久，
  而这段时间只能靠 SSE 看到 `processing`。当前没有一条可观察指标说明"排队了多久"。
- **验收要点**
  - [ ] `/api/ops/metrics` 增加"确认请求排队时长 / 在跑执行数 / 被拒次数"三项，
        采不到时给 `null` + 原因（沿用既有口径，不返回 0）；
  - [ ] 线程池饱和时返回可识别的 503 或 429，**不得**静默排队到浏览器超时；
  - [ ] 页面在长等待下不误显示成功（已有回归，补一条耗时场景）；
  - [ ] 写进 `docs/CAPACITY-RUNBOOK.md`：单实例上限怎么算、怎么看。
- **不做**：不改"确认后同步执行"这一模型（改成异步任务队列是另一场设计评审）。

### D1 · `[功能] 输入框附件入口（➕）`

- **风险**：**高** ｜ **依赖**：需先决策 ｜ **label**：`type:feature` + `question`
- **详情**：见 `PROPOSAL-CHAT-UI.md` 的 Issue 5。
- **现在为什么不能做**：服务端**没有任何上传端点**（群众侧就 6 条路径，没有 upload），
  也没有存储、大小/类型限制、保留期策略。留一个点了不工作的 ➕ 是虚假功能。
- **需先定**：是否首期做；存储落点与保留期；是否与浏览器执行器的域名白名单共用。

## 3. 需要你拍板的四件事

1. **B1**：敏感字段漏声明 → 拒绝发布（硬门禁）还是允许发布但强制全部打码？
   前者更安全但要求业务先给敏感词表；后者上线快但打码范围会过大。
2. **B1/B3**：`input_text` 要不要落库？不落（推荐）则开场用 `flow_title`；落则新增一块
   自由文本留存面，DESIGN §7 需要相应条款。
3. **A1**：`TaskView.result` 收窄会不会影响 `admin-ui` 或运维脚本？如果会，是收窄还是新增
   平行视图（`staff` / `ops` 各一份），需要你确认，因为这决定改动面。
4. **C1**：自检流程改用 `sys_clock`（最小），还是新写一个回显参数的工具（更像真实办理）？

## 4. 明确不进清单的东西（防扩散）

- 分支/并行/退回上一步的**流程引擎**——当前线性 `next_step` 是刻意的最小实现，
  放开它等于重做 `FlowRunner`，不是一条 issue；
- 双人复核与发布审批——`admin_flows.py` 明确"本轮不做双人复核，署名是唯一责任锚点"；
- OCR / 附件内容识别；
- 多租户与权限模型重构；
- 移动端专门重写（现有响应式只做核对，不重构）。

## 5. 提交建议

先提 **A 组三条**（纯修复、互不冲突、能并行），一天内可闭环；随后提 **B 组三条**（串行，
按 B1→B2→B3 顺序合并）；**C 组**在 B 组合并后提，避免同时改 `state_view.ts` 与 `main.ts`
造成冲突；**D1** 等你对第 3 节第 1、2 条定了再提。

---

## 6. 已处理项（2026-11-09 核对，勿重复提交）

| 条目 | 结论 | 落在哪里 |
|---|---|---|
| **A2** | 已修 | PR #94：管理侧表单补 `field_labels` / `intent_rules`，另加槽位正则的保存时校验 |
| **C1** | 已修（按第 3 节第 4 条的"最小"方案） | PR #93：自检流程改用内置 `sys_clock`，并新增装配期工具对账（`harness/preflight.py`） |
| **A1** | 已修，但**方案与本文验收要点不同**，见下 | PR #95 |

### A1 的实际做法与本文的差异（需要你确认是否接受）

本文原来的验收要点是：`TaskView` 新增 `result_summary: [{name,label,value}]`，
`StepDefinition` 用 `result_fields` + `result_labels` 声明白名单，群众侧不再下发 `result`。

实际落地的更轻：模型契约加一个 `answer`（群众要读的正文），步骤用 `answer_label`
（单个字符串）声明是否放行，`Runner.citizen_result()` 做白名单出口。差别在：

- **没有**新增 `result_summary` 字段，`TaskView.result` 的形状没变，只是内容变成
  `{作者声明的标签: 正文}`——所以不存在"依赖 `TaskView.result` 的调用方被破坏"的问题
  （原第 3 节第 3 条担心的正是这个）；
- **没有** `result_fields` 复数形式：目前只有一段正文，一个标签够用。要展示多个字段
  时得扩成字典，那是另一次变更；
- `resultRows()` **没改**：它本来就只渲染标量、跳过 `_` 前缀，缺的是声明机制而不是过滤规则。

如果后续确实要"多个结果字段各带标签"，再按本文原方案扩成 `result_fields` + `result_labels`，
现在的 `answer_label` 是它的退化形态，不与它冲突。
