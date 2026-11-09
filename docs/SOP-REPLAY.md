# SOP 发布前回放

流程由运维维护，但"改完对不对"以前只能上线后才知道。本文说明发布前回放怎么跑、
判什么、拦什么，以及它**不**覆盖什么。

相关代码：`domain/replay.py`（用例与断言）、`application/replay.py`（隔离执行）、
`application/release.py`（发布门禁）、`infrastructure/replay_cases.py`（用例存储）、
`scripts/replay_flow.py`（运维入口）。

## 1. 它回答什么问题

给定 `(flow_id, version)` 和一组固定用例，回放给出三条观测：

- 走了哪些步骤、每一步走完没有；
- 每个执行步骤要求调用哪个工具、参数摘要是什么；
- 任务终态是什么。

断言把观测与用例里的期望比对，得出单条用例的判定，再汇总成"这个版本能不能发"。

它**不**回答：模型话术好不好（见 [INTENT-EVAL.md](INTENT-EVAL.md) 的评测集）、
真实业务系统是否接受这笔提交（那是联调）、以及性能与容量（见
[HARNESS.md](HARNESS.md) 第 21 节）。

## 2. 四种判定

| 判定 | 含义 | 能否作为发布依据 |
|---|---|---|
| `pass` | 观测与期望完全一致 | 能 |
| `fail` | 观测与期望不一致（步骤/工具/终态/未授权工具） | 不能，且**拦发布** |
| `invalid` | 用例自身无效：缺必填字段、绑了别的版本、给了未声明的字段 | 不能，且**拦发布** |
| `unconfirmed` | 回放超时或崩溃，没有结论 | 不能，且**拦发布** |
| `aborted` | 被中断（确认轮次不收敛、运维停止） | 不进发布依据 |

`invalid` 与 `fail` 分开是刻意的：用例写错时运维该去改用例，流程回归时该去改流程。
两者混成一个 `fail`，每次改用例都会看起来像流程坏了。

`pass` 不等于"办成了"。带副作用的工具在回放里一律按 `unconfirmed` 记账（见下节），
所以一条提交类流程的正常期望是 **`terminal = outcome_unconfirmed`**。要求它 `completed`
的用例会如实判 `fail`——它问的是一件回放里不可能发生的事。

## 3. 隔离：为什么打不到真实系统

`ReplayExecutor`（`application/replay.py`）**不持有工具注册表**。它不是"记得别调工具"，
而是手上根本没有可调的对象：

- 工具名与参数只来自流程定义与任务槽位，回放把它们记成一次观测；
- 声明为只读的工具按"模拟成功"记账，其余一律 `unconfirmed` + `error_code=replay_side_effect`，
  且每条观测都带 `replay=true`；
- 每次回放使用全新的进程内任务库与确认库，跑完即丢——不会在真实任务流里留下幽灵任务；
- 回放不写生产库：用例库是唯一的持久化写入点（那是配置，不是执行结果）。

默认把**所有**工具当作有副作用（fail-closed）。只有显式声明过的工具才按只读模拟，
命令行对应 `--side-effect-free <TOOL>`。

## 4. 断言什么

| 维度 | 期望写法 | 反例会报 |
|---|---|---|
| 步骤序列 | `steps: ["collect", "lookup"]` | `step_missing` / `step_unexpected` / `step_order_mismatch` |
| 工具集合 | `tools: ["replay_read"]` | `tool_set_mismatch`（分别列出漏调与多调） |
| 终态 | `terminal: "outcome_unconfirmed"` | `terminal_mismatch` |
| 未授权工具 | 无需声明，恒定校验 | `unauthorized_tool` |

期望用 `null` 表示"这一维不约束"，用 `[]` 表示"一步都不许走 / 一次都不许调"。
两者必须都能表达，否则"这条流程不允许调用任何工具"这条最常见的负向期望就写不出来。

**未授权工具**有两层：

1. 运行期：任何一次工具调用都必须落在**它所在步骤**声明的工具上，否则判
   `unauthorized_tool`；
2. 装配期（可选，`FlowReplay(authorized_tools=...)`）：流程声明的工具若不在部署授权集合
   内，**一步都不跑**直接判 `fail`。拿模拟结果去证明"流程没问题"比不跑更危险。

参数只留在摘要（`sha256`，见 `digest_arguments`）里，报告不含群众填写的原文。因此回放
可以断言"用哪组参数调了什么"，但**不能**断言"参数值等于某个具体字符串"——那需要另一
条带脱敏审批的路径。

## 5. 用例是数据

用例与流程版本一起存库（表 `replay_cases`，主键 `(flow_id, version, case_id)`）：

- 改期望不必发版；
- 同一个业务场景在不同版本上是两条用例——改流程往往就是要改期望；
- 用例**可以删**（与不可变的流程版本不同）：期望写错了必须能改，否则运维会绕开回放
  直接发版本，那比没有回放更糟。

## 6. 发布门禁

两道门，顺序固定，都是先裁决再移动指针：

1. **识别样例**（`application/intent_eval.py`）：该版本必须带够正例（≥3）与近似误例
   （≥2），且评测里不得出现误路由、漏路由率不得超过上限。样例跟版本内容绑定
   （`FlowDefinition.intent_samples`），改样例等于改版本；
2. **回放**（本节其余部分）：该版本有回放用例时，未通过不得发布。

识别这条放在回放之前：样例不足是作者态问题，先报能省一次全量回放。细节见
[INTENT-EVAL.md](INTENT-EVAL.md) 第 3.4 节。

回放这条的规则：**该版本有回放用例时，未通过不得发布。**

- 没有用例的版本照旧可以发布。门禁不假装覆盖没有基线的流程，但"有没有基线"这件事
  在报告里明确写出来（`replay_ran=false`），不会伪装成"检查通过"；
- 有阻挡项（`fail` / `invalid` / `unconfirmed`）即拦下，**指针不动**；
- 全部用例被中断时同样不放行：那时没有阻挡项，但也**没有通过的用例**，也就没有依据；
- `apply=False` 是"只看能不能发"的预演，不写任何状态；
- 开关：`WORKPANDA_REPLAY_RELEASE_GATE`（默认开启）。关掉它意味着用例只是摆设，
  而这恰好是最难被发现的失效方式。

判定与移动指针是两个存储操作：先裁决，再移动。门禁失败时不会出现"指针移了、回放没过"
的中间态。

## 7. 运维用法

```bash
# 只看能不能发（不改状态）：识别样例与回放都会跑，退出码 0=通过 1=有阻挡项
python3 scripts/replay_flow.py --flow demo.generic --version 2

# 加一条用例（JSON 只写用例本身，flow_id/version 取自命令行）
python3 scripts/replay_flow.py --flow demo.generic --version 2 --add-case case.json

# 通过门禁后真的移动发布指针（必须署名，--actor 与 --publish 一起必填）
python3 scripts/replay_flow.py --flow demo.generic --version 2 --publish \
    --actor ops.zhang --side-effect-free sys_clock
```

`case.json`：

```json
{
  "case_id": "happy",
  "slots": {"subject": "自检"},
  "expect": {
    "steps": ["collect_input", "execute_demo"],
    "tools": ["sys_clock"],
    "terminal": "outcome_unconfirmed"
  },
  "note": "最小正常路径"
}
```

脚本只读一个库文件路径（`--sqlite` 或 `WORKPANDA_SQLITE_PATH`），刻意不构造 `Settings`：
这个入口不该顺带读取 `.env` 里的模型凭据。

## 8. 已验证 / 未验证

单测覆盖（`tests/test_replay.py`，26 条）：

- 正常路径：逐 step 结论（`completed`/`paused`/`incomplete`）与总判定 `pass`；
- 缺参：停在收集阶段判 `invalid`，不判 `fail`；
- 超时 / 崩溃：判 `unconfirmed`，绝不降级成 `pass`；
- 中断：判 `aborted`，`publishable=false`，且不计入阻挡项（`blocking=()`）；
- 步序：漏调 / 多调 / 错序分别报三种不同 code；
- 未授权：断言器层面的越界调用、以及装配期"声明了未授权工具则一步不跑"；
- 隔离：给容器注入一个会记账的生产执行器，回放跑完后它**零调用**；
- 可重复：同一版本 + 同一用例跑两次，步骤、工具、参数摘要、终态逐字节相同；
- 门禁：通过则移动指针，未通过则指针不动（并带报告抛出），无用例时 `replay_ran=false`；
- 用例库：内存与 SQLite 两种后端的新增/重复/删除/按版本隔离；
- 脚本：写错期望时退出码 1 并打印 `FAIL` 与具体 code；`--add-case` 后可重放到 `PASS`。

未验证：

- 用历史 trace 直接当回放输入（trace 只有参数摘要与次数，字段齐全度未评估，见下节）；
- 多实例并发回放同一个版本（用例库与流程库都在同一个 SQLite 文件里，靠 `busy_timeout`
  串行化，未做压测）；
- 真实业务工具的副作用账本与回放结论的交叉核对。

## 9. 已知限制

- **回放观测的是"步骤要求的工具"，不是"模型实际调了几次"**：次数由模型决定，属于
  话术质量问题；回放刻意不调模型，因此同一版本的回放结论不会随模型变化而漂移。
  "模型有没有按预期调用工具"由 `docs/INTENT-EVAL.md` 的评测集与 harness trace 负责；
- 报告不持久化：门禁用的报告由调用现场返回，不进库。需要留档时应把 JSON 输出落到
  归档目录（`harness_audit_path` 那套只追加通道不覆盖回放）；
- 参数只留摘要，无法断言具体取值（脱敏要求下的取舍）；
- 回放不认识"跨任务的时序"：一条用例只跑一个任务，不模拟多任务并发、取消与恢复；
- `aborted` 的门槛是确认轮次上限（默认 16）：流程若在确认与执行之间来回震荡，会被判
  中断而不是判失败——这类流程应先用状态机用例盯住。
