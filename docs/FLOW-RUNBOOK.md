# 事项上线 runbook（运维：从一条新业务到线上跑通）

> 读者：负责维护流程定义的人（不改代码）。按本文顺序走，可以独立把一个新的办事事项
> 发上线；出事时也知道该回滚、该停用，还是该发新版本。
>
> 不在本文范围内：回放判定原理见 [`SOP-REPLAY.md`](SOP-REPLAY.md)；凭据、绑定地址与
> CORS 见 [`OPS-ACCESS-CONTROL.md`](OPS-ACCESS-CONTROL.md)；管理侧权限矩阵与界面
> 行为见 [`ADMIN-CONSOLE.md`](ADMIN-CONSOLE.md)；身份核验见
> [`STAFF-IDENTITY.md`](STAFF-IDENTITY.md)。

---

## 0. 什么叫"跑通"

不是接口返回 200，也不是界面上出现了一个新条目。是这样一条完整链路：

1. 群众说一句自己的话（不是勾选项），
2. 系统把它认到**这一个**事项上（不是别的相邻事项，也不是"没听懂"），
3. 只问办事真正需要的那两三样东西（不是身份证号，不是一堆表单），
4. 调一次工具拿到可核验的依据（指南原文、回执号、状态页），
5. 给出一段结论并注明来源与核对日期，
6. 群众看到的是"办结 / 待确认 / 转人工"三者之一，且措辞与真实情况一致。

六步缺任何一步，都只是"看起来通了"。第 6 步最容易假通过：状态是 `completed` 但文案
在暗示"已经帮你办好了"——只有真的拿到回执才算办成。

---

## 1. 开工前必须落实的三件事

| 项 | 为什么必须先确认 | 怎么确认 |
|---|---|---|
| 事项名 + 版本号 | `(flow_id, version)` **不可变**，写进去就改不了，只能再建一版 | 先定名，例如 `subsidy.xxx_checklist` / `1` |
| 步骤要调的工具已经在进程里 | 运维改不了工具注册（那是代码）。名字写错不会报错，见 §5 坑 2 | 问研发：`tool` 的英文名在 `harness/builtin.py` 的 `default_registry()` 里注册过没有 |
| 管理侧凭据 | 写操作只有 `admin` 角色能做，`viewer` 只能看和跑回放 | `WORKPANDA_ADMIN_TOKENS='duty01:viewer:<随机串>,sop01:admin:<随机串>'` |

凭据的两条硬约束：

- **署名取凭据的主体名**（`sop01`），不读请求体。请求体里自己填 `published_by` 会被
  忽略。所以主体名要能对上人：写 `sop.zhang`，不要写 `admin1`。
- **密钥至少 16 个字符**，短了服务**起不来**（不是启动后拒绝登录）。实测报错是
  `OpsAccessConfigError: 主体 'duty01' 的密钥过短，至少 16 个字符`。
- 三套凭据互不通用：管理侧 `X-WorkPanda-Admin-Token` 不是运维只读
  `X-WorkPanda-Ops-Token`，也不是群众会话。拿错头会得到 401/403，不会"降级放行"。

没配置 `WORKPANDA_ADMIN_TOKENS` 时，`/api/admin/flows/*` 全部 404（不是 401）——
这是刻意的：没开的接口不该告诉外面"这里有个接口"。

---

## 2. 六步主流程（速查）

| 步 | 动作 | 通过判据 | 不通过怎么办 |
|---|---|---|---|
| S1 | 定这个事项走到哪一档（能提交 / 引导自办 / 转人工） | 结论写在事项说明里 | 见 §3.1，**先停下来**，别硬写 execute |
| S2 | 建草稿版本 `POST /api/admin/flows` | 201，返回体里 `status: draft` | 409=版本重名；422=看 `detail.fields[].loc` |
| S3 | 补识别样例（同一次请求里带上，或新版本带上） | ≥3 正例、≥2 近似误例 | 样例是版本内容，改样例=新版本 |
| S4 | 写回放用例 `POST .../cases` | 至少 1 条正常路径 | 409=用例重名，先 DELETE 再 POST |
| S5 | 预演（两道门都跑） | `published:false` + `replay_ran:true` + 全 `pass` | 409 两个错误码含义不同，见 §3.5 |
| S6 | 发布 `.../publish` | 200 且 `published_by` 是你 | 门禁拦下=指针没动，任务仍是旧版 |
| S7 | 上线后观察（建议 24h） | §3.7 三条判据都不报警 | 按 §6 决策树处置 |

下面逐步展开。命令都假设服务在 `127.0.0.1:8000`，把它们换成实际地址即可。

```bash
export WP_ADMIN='sop01:admin:<随机串>'      # 只在这台机器上、只在当前 shell
export H="X-WorkPanda-Admin-Token: $WP_ADMIN"
```

---

## 3. 逐步怎么点

### 3.1 S1：先定这个事项走到哪一档

系统现在能做的事分三档，**默认选 B**：

| 档 | 系统做什么 | 适合什么事项 | 现状 |
|---|---|---|---|
| A | 代群众提交（填表 + 点提交） | 极少数、且已拿到书面授权对接的站点 | **未开放**。浏览器执行器只开到"进入填写页"，点提交是禁区 |
| B | 把该说的说清楚：材料清单、办理路径、去哪儿办、回执怎么查 | 绝大多数事项 | 已可用 |
| C | 明确告诉群众"这件事要人来办"，转人工并留痕 | 需要面签、核验原件、政策口径有争议的 | 已可用 |

判断方法一句话：**如果你的 execute 步骤需要点"提交"，就停在这里**，不要自己往
`HARNESS_BROWSER_PERMITTED_CLICK_TESTIDS` 里加白名单。那个白名单是给"上传材料后点确认"
这类已单独评审过的动作留的，不是给"让流程跑得更顺"用的。

占大多数的事项应当是 B 档：它的产出是**信息**（清单、路径、状态、回执查询方式），
不是"替你办完"。这不是能力妥协——代提交一旦出错，后果落在群众身上，而回执号回查
这类事系统做得比人稳。

### 3.2 S2：建草稿版本

```bash
curl -sS -X POST http://127.0.0.1:8000/api/admin/flows \
  -H "$H" -H 'Content-Type: application/json' \
  -d @flow.json
```

`flow.json` 最小形状（示例是"灵活就业社保补贴·材料清单自查"的第 2 版，可对照
`src/workpanda/infrastructure/pilot_flows.py` 里的第 1 版看差异）：

```json
{
  "flow_id": "pilot.flex_subsidy_checklist",
  "version": "2",
  "title": "灵活就业社保补贴·材料清单自查",
  "steps": [
    {
      "step_id": "collect_application",
      "kind": "collect",
      "title": "告知申请身份与已备材料",
      "required_fields": ["applicant_type", "prepared_materials"],
      "prompt": "请告诉我两件事：打算以哪种身份申请，以及手上已经有哪些材料。不需要提供任何证件号码。",
      "field_labels": {
        "applicant_type": "申请身份",
        "prepared_materials": "已准备的证明材料"
      },
      "sensitive_fields": []
    },
    {
      "step_id": "lookup_checklist",
      "kind": "execute",
      "title": "对照材料清单",
      "tool": "policy_checklist",
      "instruction": "调用 policy_checklist，参数 topic 必须是 flexible_employment_social_insurance_subsidy，不要自己拼写主题名；把工具返回的清单原文写进 evidence，这是本步骤唯一依据；answer 按三段写：①清单原文 ②群众提到已备的哪几项没在清单里 ③提醒以就业困难人员身份申请前需先完成认定。不得判断是否符合条件，凡涉及符合与否只能表述为「由经办机构核定」。",
      "requires_confirmation": true,
      "confirm_message": "即将按你填写的身份与已备材料查询材料清单，请确认。",
      "answer_label": "材料清单自查结果",
      "fail_to_handoff": true
    }
  ],
  "intent_samples": [
    {"sample_id": "pos_a", "category": "positive", "text": "我想查一下灵活就业社保补贴要带什么材料"},
    {"sample_id": "pos_b", "category": "positive", "text": "灵活就业社会保险补贴申报材料有哪些"},
    {"sample_id": "pos_c", "category": "positive", "text": "灵活就业社保补贴需要哪些材料"},
    {"sample_id": "near_pension", "category": "near_miss", "text": "灵活就业人员怎么参加养老保险"},
    {"sample_id": "near_unemployment", "category": "near_miss", "text": "我要申请失业保险金"},
    {"sample_id": "oos_weather", "category": "out_of_scope", "text": "今天天气怎么样"}
  ],
  "intent_rules": [
    {
      "intent_id": "pilot.flex_subsidy_checklist.materials",
      "flow_id": "pilot.flex_subsidy_checklist",
      "flow_version": "2",
      "keywords": ["灵活就业社保补贴", "灵活就业社会保险补贴"],
      "slot_patterns": {}
    }
  ]
}
```

建版本时必须知道的六件事：

1. **版本不可变。** 同一 `(flow_id, version)` 已存在一律 409，不覆盖。改一个字也是
   新版本（`2` → `3`）。这是为了"线上跑的到底是哪一版"能一句话答清。
2. **请求体没有 `status` 字段。** 服务端强制草稿；只有发布指针指到它，才叫已发布。
   你写进去的 `status` 会被忽略。
3. **步骤三类，各有硬规矩**：`execute` 必须带 `tool`；`collect`/`handoff` **不得**带
   `tool`、不得 `requires_confirmation`、不得 `answer_label`。写错都不会"静默降级"，
   直接 422 并告诉你字段位置。
4. **`field_labels` / `answer_label` / `sensitive_fields` 都归作者填。** 平台不猜，
   因为平台不知道哪个字段叫"申请身份"、哪个是证件号。留空不会报错——群众会看到
   字段名本身（`applicant_type`），难看但诚实；标错则不生效，见 §5 坑 4。
5. **`intent_rules.flow_version` 必须填自己这一版**（不是上一版、不是留空）。
   规则随版本走，发布一版等于同时换掉识别口径。
6. **`handoff` 步骤排在正常路径上，就意味着正常路径走到这里结束。** 它不是"最后兜底
   的一句话"，而是"到此交给人工，自动化停止"。所以上例没有放 handoff 步骤：失败
   转人工由执行步骤的 `fail_to_handoff: true` 负责，不需要（也不该）再挂一个空的
   handoff 步骤——那会让每一条正常路径都以 `handoff_required` 收尾，回放判
   `TERMINAL_MISMATCH`，而你会以为是用例写错了。

422 的读法：

```json
{"detail": {"code": "admin_draft_invalid", "detail": "流程草稿校验未通过。",
            "fields": [{"loc": ["steps", 1, "tool"],
                        "message": "execute step 'lookup_checklist' must declare a tool"}]}}
```

`loc` 就是你在 JSON 里的路径，可以直接定位到第 2 个步骤的 `tool` 字段。

### 3.3 S3：补识别样例

样例是**发布的前置材料**，不是可选项。三类各只断言一件事：

| 类别 | 断言 | 下限 | 写成什么 |
|---|---|---|---|
| `positive` | 必须被识别到**本事项的这一版** | 3 | 三种常见说法（口语、书面、极短） |
| `near_miss` | 不得被识别到**任何**事项 | 2 | 话题相邻但该去别处的话 |
| `out_of_scope` | 不得被识别到任何事项 | 0（建议 1） | 寒暄、无关请求 |

三条写作纪律，都是踩过的坑：

- **关键词用完整事项名，不要用半截词。** 路由只做关键词计数，没有"并且"语义。用
  "社保补贴"会把"企业吸纳社保补贴""失业保险金"一起捞进来，而误路由的代价不是多问
  一句，是群众的话被塞进另一个事项的执行参数里。
- **近似误例要挑"最可能被误捞"的那类。** 不是随便挑两句无关的话——那测不出东西。
  上面例子里两条都含"灵活就业"、都不含"社保补贴"，专门用来证明关键词没放宽。
- **只写合成话术，不写群众原话。** 样例随版本入库，还会进评测报告与 CI 日志；而且
  报告里只出现 `sample_id` 与类别，不出现原文——原文一旦写进去就再也收不回来。

条数下限是**下限不是目标**。真正的覆盖度由事项责任人补：他见过群众怎么问，运维没有。

### 3.4 S4：写回放用例

回放 = 拿假执行器把这版流程重跑一遍。它不碰真实系统（执行器压根不持有工具注册表），
也不写生产库，所以可以随便跑。用例跟着版本存库。

```bash
curl -sS -X POST \
  http://127.0.0.1:8000/api/admin/flows/pilot.flex_subsidy_checklist/versions/2/cases \
  -H "$H" -H 'Content-Type: application/json' \
  -d '{
    "case_id": "happy_path",
    "slots": {"applicant_type": "就业困难人员", "prepared_materials": "身份证、户口簿"},
    "expect": {"steps": ["collect_application", "lookup_checklist"],
               "tools": ["policy_checklist"],
               "terminal": "outcome_unconfirmed"},
    "note": "正常路径：两项都说了。终态是 outcome_unconfirmed 而不是 completed，原因见下"
  }'
```

写用例的五个要点：

1. **`slots` 只放"群众会这样填"的字段值**，字段名必须是这版流程声明过的。
2. **`expect` 三个维度独立，`null` 才是不约束。** `"tools": []` 是"一次工具都不许调"，
   `"tools": null` 才是"随便调"。这两个别写混——负向期望全靠这个区别表达。
3. **`flow_id` / `version` 取自路径，不写进请求体。** 写进请求体也会被覆盖，这是防止
   用例绑错版本（绑错版本会被判 `invalid`，界面上看起来像"流程坏了"）。
4. **用例在版本内唯一，不会覆盖。** 改期望请先 `DELETE .../cases/<case_id>` 再 `POST`，
   或者直接发新版本（往往后者更对：改期望通常意味着流程也改了）。
5. **正常路径的终态写 `outcome_unconfirmed`，不要写 `completed`。** 这条最反直觉，
   实测踩过：回放把**每一个**工具都按"可能有副作用"处理（fail-closed，不声明就按
   危险算），执行步骤因此以「未确认」收尾。写 `completed` 会被判
   `terminal_mismatch`，而这不是流程坏了。详见 §3.5 的实测拦截。

关于"负向用例"要说实话：**"故意少填一个字段，看它是否停下来问"这种用例做不了。**
回放发现必填字段缺失会直接判 `invalid` + `case_missing_fields`（"用例写错了，不是
流程回归"），不会给你一个"停在收集步骤"的通过判定。同理，`waiting_for_user` 也不是
合法的回放终态，写进 `expect` 会直接 422。

所以回放能测的主要是：**步骤序列、调用了哪些工具、有没有越权工具、终态属于哪一类**。
输入层面的边界（少填、填错、超长）目前没有回放通道，只能靠线上观察与人工走查——
这一条写进 §8 的缺口，不要在这里许愿。

### 3.5 S5：预演（不许跳过）

预演跑两道门，**不改任何状态**：

```bash
# 只出回放报告（样例还没就位时先写用例用；不能当发布依据）
curl -sS -X POST .../versions/2/replay -H "$H"

# 走完整门禁的预演：识别样例 + 回放都跑，指针不动
curl -sS -X POST http://127.0.0.1:8000/api/admin/flows/pilot.flex_subsidy_checklist/publish \
  -H "$H" -H 'Content-Type: application/json' \
  -d '{"version": "2", "apply": false}'
```

预演通过的返回里三个字段都要对（`report` 是逐用例明细，这里省掉了）：

```text
flow_id      "pilot.flex_subsidy_checklist"
version      "2"
published    false            ← 预演不移动指针
replay_ran   true             ← 真的跑了回放，不是"没东西可评"
report       { "verdict": "pass", "publishable": true, "results": [...] }
message      "预演通过，尚未发布（发布指针未移动）"
published_by null             ← 预演不署名
```

`published_by` 是 `null` 而不是你的名字——预演没有移动指针，不该留下"某人发过"的
记录。

被拦时是 409，两个错误码对应的补救动作完全不同，别混：

| 错误码 | 含义 | 看哪 | 补救 |
|---|---|---|---|
| `admin_intent_gate_blocked` | 识别样例不够 / 有误路由 / 有漏路由超限 | `detail.report.coverage`（每类 `have/need`）、`matrix` | 补样例、收窄关键词 |
| `admin_release_blocked` | 回放有阻挡判定 | `detail.report.results[].findings[].code` | 改流程或改用例（见下表） |

回放判定怎么读（详见 `SOP-REPLAY.md` §2，这里只给动作）：

| 判定 / 问题码 | 是流程坏了还是用例写错了 | 该动哪里 |
|---|---|---|
| `pass` | — | 不动 |
| `fail` + `STEP_ORDER_MISMATCH` / `STEP_MISSING` | 流程坏了 | 改步骤定义 |
| `fail` + `TOOL_SET_MISMATCH` | 流程坏了（或工具名写错） | 改 `tool` / `instruction` |
| `fail` + `UNAUTHORIZED_TOOL` | 流程坏了：步骤调了没声明的工具 | 改步骤声明，不要放宽 |
| `fail` + `TERMINAL_MISMATCH` | **多半是用例写错了**。最常见：带工具的正常路径写了 `completed`（应为 `outcome_unconfirmed`） | 先用例，仍不符再改流程 |
| `invalid` + `CASE_MISSING_FIELDS` | 用例写错了：必填字段没给全，回放停在收集阶段 | 补全 `slots`，别去改流程 |
| `invalid` + `CASE_FLOW_MISMATCH` / `CASE_UNKNOWN_FIELD` | 用例写错了：绑错版本，或给了这版没声明的字段 | 改用例 |
| `unconfirmed` / `aborted` | 超时、崩溃或被中断，**没有结论** | 重跑；连续超时找研发 |

`unconfirmed` 不是通过。门禁把它当"没过"处理，这是对的：没有证据就不能放行。

用错终态时的真实拦截长这样（本文档实测，一字未改）：

```text
409 admin_release_blocked
回放未通过，已拦下 pilot.flex_subsidy_checklist@2 的发布：
happy_path=fail（terminal_mismatch）。整体判定 fail

"findings": [{"code": "terminal_mismatch",
              "detail": "期望终态 completed，实际 outcome_unconfirmed"}]
"steps": [{"step_id": "collect_application", "outcome": "completed"},
          {"step_id": "lookup_checklist", "kind": "execute", "outcome": "incomplete"}]
"tools": [{"name": "policy_checklist", "side_effecting": true, ...}]
```

读法：步骤都走到了、工具也调了（`side_effecting: true` 是回放的默认记账），只有终态
对不上。这时候要改的是用例里的那一行，不是流程。

### 3.6 S6：发布

```bash
curl -sS -X POST http://127.0.0.1:8000/api/admin/flows/pilot.flex_subsidy_checklist/publish \
  -H "$H" -H 'Content-Type: application/json' \
  -d '{"version": "2"}'
```

发布 = 把指针移到这一版。返回 200 且 `published_by` 是你，才算发上了：

```text
{"flow_id": "pilot.flex_subsidy_checklist", "version": "2", "published": true,
 "replay_ran": true, "published_by": "sop01",
 "message": "已发布，群众侧下一次创建任务即使用该版本"}
```

注意这句 `message` 的措辞：**下一次创建任务**。已经在跑的任务不换版本。

发布后**立刻做一次群众侧冒烟**，这是唯一能证明"发布生效"的动作：

1. 在群众侧说一句正例里的话；
2. 看回到的事项名是不是这一版（标题变了没）；
3. 走一遍"少说一个字段"，看它是不是停下来问，而不是自己编一个值。

两条容易被忽略的事实：

- **没有回放用例的版本也能发布**，响应里是 `replay_ran: false`。这不是"通过"，是
  "没证据"。除非是紧急止血的回滚，否则不要留这种版本在线上。
- **发布不会打断在途任务。** 指针变了，已经在跑的群众仍按他开始时的那一版推进到底。
  这是刻意设计（回滚不该把人卡在半路），但也意味着"发布后立刻所有人都用新版"是错的
  预期：完成时间按任务时长滞后一个批次。

### 3.7 S7：上线后 24 小时看什么

三个只读接口，用 `X-WorkPanda-Ops-Token`：

```bash
curl -sS http://127.0.0.1:8000/api/ops/flows/releases   # 现在跑哪一版、谁发的
curl -sS 'http://127.0.0.1:8000/api/ops/traces'         # 逐任务的执行留痕
curl -sS 'http://127.0.0.1:8000/api/ops/traces/summary' # 汇总
```

三条判据，任何一条不达标就按 §6 处置：

1. **`outcome_unconfirmed` 占比**明显高于平时 → 工具拿不到依据（站点改版、参数错、
   工具名没注册）。这一类**不能**靠"再点一次"解决。
2. **`handoff_required` 集中**在同一个步骤 → `instruction` 写得不清楚，模型在里面
   反复试到超步数，或者该转人工的判定条件太松。
3. **识别错了**：群众说 A 事项，落到了 B。这类问题回放抓不到（回放按用例指定输入
   跑，不走真实路由），只能靠运维看 `traces` 里的 `flow_id` 与原始输入对不对得上。

---

## 4. 三种"下线"动作，别用错

| 动作 | 语义 | 门禁 | 在途任务 | 什么时候用 |
|---|---|---|---|---|
| 回滚 `.../rollback` | 指针移回某个历史版本 | **不走**门禁 | 各自跑完 | 新版上线后发现问题，要立刻回到上一版 |
| 停用 `.../disable` | 在指针行打停用标记，历史版本与署名一行不删 | — | 各自跑完 | 事项本身要下线（政策变了、暂停受理） |
| 发新版本 | 建 vN+1 再发布 | 走门禁 | 各自跑完 | 改文案、改步骤、改关键词——**日常都走这条** |

回滚不走门禁是刻意的：要回滚的那个版本当初是通过门禁发上去的，内容一个字没变，
再评一遍只会让止血变慢。代价是——**回滚不是"验证过的"，是"上次发过的"**。

停用与回滚都会记录署名（`disabled_by` / `published_by`），事后能查到是谁动的。

**停用是硬断，不是优雅下线。** 停用后群众再说这个事项，拿到的是
`422 flow_not_published`——一个错误码，不是一句"这项业务暂停受理了"。所以停用前如果
还需要对外解释，得同时改群众侧的告知渠道（页面文案、社区通知）；系统本身不会替他
说这句话。

```bash
# 回滚
curl -sS -X POST .../pilot.flex_subsidy_checklist/rollback -H "$H" \
  -H 'Content-Type: application/json' -d '{"version": "1"}'

# 停用
curl -sS -X POST .../pilot.flex_subsidy_checklist/disable -H "$H"
```

---

## 5. 四个"看起来成功、其实没通"的坑

这四条都不是理论风险，是已经踩过的。

### 坑 1：用命令行脚本发布，运行中的进程没重载

`scripts/replay_flow.py --publish` 直接改库。但"发布后让群众立刻用新版"这一步
（`FlowRegistry.reload()`）只发生在管理侧 HTTP 接口里。所以：

- **现象**：`GET /api/admin/flows` 显示指针已经是你发的那一版，群众侧却还是旧版。
- **原因**：进程内存里还是旧目录。
- **这条脚本该用来做什么**：写用例、跑预演、看报告（这些都不改状态，或在隔离库里跑）。
- **发布请走 HTTP 接口**；一定要用脚本发，发完重启服务，并重新做 §3.6 的群众侧冒烟。

### 坑 2：工具名写错，回放查不出来

回放执行器**不持有工具注册表**（这是它"打不到真实系统"的实现方式），所以它无法
告诉你 `tool` 这个名字在进程里到底存不存在。而启动期对账只核对**已发布**版本、
且只在**启动时**跑一次。

- **现象**：步骤拿着空授权跑完，结果是 `succeeded`，轨迹里没有一条 `tool_calls`。
- **为什么危险**：三态契约里"没有依据就该是 `unconfirmed`"，这里却因为"压根没有
  工具"被降级成 `succeeded`——看起来跑通了，其实一个依据都没有。
- **对策**：建版本前就跟研发对一次工具名；上线后第一个任务就去看 `traces` 里有没有
  `tool_calls`。

### 坑 3：没有用例 = 没证据，不报错

`replay_ran: false` 是"这道门没东西可评"，不是"评过了"。默认开关是开着的
（`WORKPANDA_REPLAY_RELEASE_GATE`），但它只能在**有用例**时起作用。

### 坑 4：标了打码字段，但名字写错，于是永远不打码

`sensitive_fields` 里写了 `id_card`，可这一步声明的字段叫 `holder_no`——不报错，
那个字段就一直明文回显。而写下这一行的人已经认为"这件事处理过了"，比从没标过更
危险：没标的人还会自己看一眼明文。

- **对策**：`sensitive_fields` 只写本步骤 `required_fields` 里真实存在的字段名；
  上线冒烟时**故意填一个假证件号**，确认页面显示的是打码后的样子。

---

## 6. 出事时的决策树

先说一句原则：**能止血先止血，再查原因。** 查原因可能要一小时，群众就在这一小时里
一直撞墙。

```
群众反馈"办不了 / 看不懂"
├── 是识别错了吗（说 A 落到 B，或一直被要求重说）
│   ├── 是 → 不要改线上这一版（改不了）。建 vN+1，只去掉过宽的关键词，
│   │        补一条对应的 near_miss 样例，走 S5 → S6
│   └── 否 → 往下
├── 是问得太多 / 问的字段拿不到吗
│   ├── 是 → vN+1 收窄 required_fields。注意：少收一项就少一条要定留存与删除的路径，
│   │        收窄永远是安全方向
│   └── 否 → 往下
├── 是答非所问 / 答里出现"你符合条件"这种越权结论吗
│   ├── 是 → vN+1 改 instruction，把"判断资格"明确禁掉，引用必须来自工具 evidence
│   └── 否 → 往下
└── 是明明办不了却没说"要人来办"吗（没有转到人工）
    ├── 是 → vN+1 检查 fail_to_handoff 与 handoff 步骤是否存在
    └── 否 → 拿 task_id 去 /api/ops/traces 看逐 step 留痕，再判断
```

要**立刻**止血、来不及发新版的两种情况：

- 新版刚发上去就发现大范围不对 → `rollback` 回上一版（最快，不评）。
- 事项本身不该继续接单 → `disable`。

两条都不要指望"改库里那一版"——版本不可变，没有这条路径。

---

## 7. 一页验收清单

发布前逐项打勾，缺一项就别发：

- [ ] `(flow_id, version)` 定好了，且与上一版不同名
- [ ] 步骤里每个 `execute` 的 `tool` 都跟研发核对过，确认进程里有
- [ ] `collect`/`handoff` 没有带 `tool` / `requires_confirmation` / `answer_label`
- [ ] `intent_rules.flow_version` 指向本版
- [ ] 正例 ≥3、近似误例 ≥2，且近似误例是"最可能被误捞"的那类
- [ ] 样例全是合成话术，没有群众原话
- [ ] 回放用例 ≥1 条正常路径，终态写的是 `outcome_unconfirmed`（不是 `completed`）
- [ ] 负向期望用 `null` / `[]` 表达对了；没有拿"少填字段"当负向用例
- [ ] 预演通过：`published:false`、`replay_ran:true`、整体 `pass`
- [ ] 发布响应里 `published_by` 是你
- [ ] 群众侧冒烟：一句话进来 → 落对事项 → 少说一项会停下来问 → 结论带来源

---

## 8. 已知缺口（当前版本的真实边界）

写在这里是为了避免"以为有"：

- **没有双人复核。** 谁有 `admin` 凭据，谁就能一个人建版、预演、发布、回滚。署名
  只能回答"是谁"，不能回答"另一个人同意过"。角色分工的设计草案尚未落库。
- **回滚不验证。** 见 §4，回滚是"上次发过的"，不是"这次验证过的"。
- **回放的断言范围有限。** 四件事：步骤序列、工具集合、有无越权工具、终态。它**不**
  覆盖：确认凭证是否真的被强制、模型调了几次工具、页面长得对不对、识别是否走对。
- **回放证明不了"办成了"。** 因为每个工具都按有副作用记账（fail-closed），凡是调到
  工具的用例终态只能是 `outcome_unconfirmed`。所以"通过"的含义是"按声明的步骤走到
  底、调了该调的工具"，**不是**"结果正确"。结果正确只能靠线上留痕与人工抽检。
- **输入边界没有回放通道。** 少填、填错、超长这些输入层面的情况，回放一律判
  `invalid`（`CASE_MISSING_FIELDS`），无法当作回归断言。这类边界目前只能人工走查。
- **识别错误抓不到回归。** 回放按用例指定输入跑，不过路由。识别问题只能靠样例门禁
  （发布时）与线上观察（发布后）。
- **工具的"存在性"没有运行时兜底。** 见坑 2，只在启动时对账已发布版本。
- **管理侧界面的域名/端口/证书未定稿**（见 Issue #53）。当前默认只监听回环。

---

## 9. 这份文档实测到什么程度

写文档的人自己跑过一遍，范围如下（2026-11-09，本机 sqlite 后端）：

| 项 | 结果 |
|---|---|
| S2 建草稿（本文示例 JSON） | 201 |
| 重复建同一版 | 409 `admin_flow_conflict` |
| S4 加回放用例 | 201；重复加同一用例 409 |
| S5 预演（`apply:false`） | 200，`published:false`、`replay_ran:true`、`verdict:pass`、`published_by:null` |
| 用错终态（写 `completed`） | 409 `admin_release_blocked` + `terminal_mismatch` |
| 故意少填必填字段的用例 | `invalid` + `case_missing_fields`（不能当负向回归） |
| `waiting_for_user` 写进 `expect.terminal` | 422（不是合法终态） |
| viewer 尝试发布 | 403 `admin_forbidden` |
| S6 发布 | 200，`published_by` 取凭据主体名 |
| 回滚到 v1 | 200，指针移回，v2 变为未发布 |
| 停用 | 200，`disabled_by` 落库 |
| 停用后群众侧创建任务 | 422 `flow_not_published` |

### 9.1 群众侧真实模型执行（2026-11-10 补测）

上面那张表全是管理侧与回放。**"群众说一句话 → 模型真的干活 → 拿到有来源的结论"**
这一段另外单独跑过，用的是 `.env` 里的真实模型（DashScope 兼容接口，
`WORKPANDA_MODEL_NAME` 指定的模型），不是 mock。

| 项 | 结果 |
|---|---|
| 建任务（"我想查一下灵活就业社保补贴要带什么材料"） | 201，`awaiting_input`，待填 `applicant_type`、`prepared_materials` |
| 回答两项后 | `waiting_for_confirmation`，确认提示带原文摘要 |
| 确认后（连跑 5 轮） | **5/5 `completed`**，5.5–7.3 秒 |
| 产出 | `result` 顶层唯一键是 `材料清单自查结果`（即 `answer_label`，不是 `answer`） |
| 产出的样子 | 三段：①指南里的材料清单原文 ②逐项对照群众已备材料，指出"社保卡不在清单中，是否需提交请向经办机构确认" ③两条易漏提醒（就业困难人员需先认定；补贴与失业保险待遇不能同时享受），结尾写明"是否符合申请条件由经办机构核定" |
| 成本 | 约 2860 tokens、2 次模型调用、1 次工具调用 |

这一轮同时验证了两条设计意图真的落地，不是纸面承诺：

- **清单来自工具，不是模型记忆。** 工具返回什么就说什么，模型没有自行补材料名。
- **不确定的地方承认不确定。** 社保卡那条是"清单里没有、要不要交你去问"，不是替经办机构拍板。

模型不可用时也没有降级成"办成了"：终态 `outcome_unconfirmed`，事件里
`failure_code=connect_timeout`、`tool_calls=[]`，群众侧文案是
"状态待确认 / 暂时无法核实结果，请不要重复提交，可查询进度或转人工"。
**三态契约守住了**——这正是它存在的意义。

### 9.2 环境相关的坑：连接超时可能被代理吃掉

同一台机器、同一个连不上的端点，报出来的错误取决于**走不走代理**：

| 出网方式 | 实际异常 | 是否重试 | 实测 |
|---|---|---|---|
| 直连（`WORKPANDA_MODEL_TRUST_ENV=false`） | `ConnectTimeout` | 重试 1 次后判 `model_error` | `model_calls=2`，6.5 秒（3 + 0.5 退避 + 3） |
| 走代理（`true`，默认） | `ReadTimeout` | **不重试**，判 `model_timeout` | `model_calls=1`，3.0 秒 |

原因不难理解：走代理时 TCP 是连到**本机代理**的（秒连），代理再去连目标；目标连不上
时，客户端看到的是"请求发出去没回来"，也就是读超时。而读超时按设计**不重试**——
请求可能已经在服务端算了，重试等于同一份算力花两遍。

所以：**出口有代理/网关时，连接超时这条重试可能一次都用不上**，真正该查的是代理
本身可用不可用，以及 `WORKPANDA_MODEL_TIMEOUT_SECONDS` 给得够不够。

顺带记一个旧版本的特征：2026-11-10 之前的实现在连接阶段超时上**不重试**，且连接上限
被硬编码成 10 秒。表现是**任务恰好 10 秒判 `model_timeout`、`model_calls=1`**，
而 120 秒的总预算一次都没用上。在新版本上看到这个特征，先确认有没有真的换上新代码。

**没实测的**（别把这份文档当成这些也已验证）：

- **真实模型只跑过 B 档、单实例、本机。** 见 9.1：链路通了，但没有在 A 档（代提交）、
  浏览器执行器、多实例、真实政务系统对接下跑过。
- **没有碰浏览器执行器**。本文所有事项都落在 B 档（给信息、不代提交），A 档本来就
  没开放。
- **没有多实例**。`reload` 只在单进程内生效；多实例部署下"发布是否对所有实例生效"
  未验证（与 Issue #53 的扩容共用同一个待确认项）。
- **出网只在两种方式下跑过**：本机直连，与 WorkBuddy 沙箱代理。正式环境的
  代理/网关/白名单没有验证，见 9.2 的坑。
