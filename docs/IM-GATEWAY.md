# 企业微信网关（N4）

> 范围：Issue #64 的落地说明——入站回调的验签、幂等与归一化，出站任务卡片的投递，
> 以及"网关不改任务状态"这条边界的实现形状。
> 结论先行：**网关是通道**（D2）。它把外部消息变成内部事件、把内部状态变成一张卡片，
> 两件都不涉及任务状态本身；它拿到的任务视图只有 `get()`。

## 1. 边界与结论

| 问题 | 结论 |
|---|---|
| 网关能改任务状态吗 | **不能**。注入的是 `ReadOnlyTasks`，只有 `get()`；模块里没有 `.commit(` / `.append_event` 调用（AST 断言） |
| 网关能签发确认凭证吗 | **不能**。IM 文本没有落点，见 [STAFF-IDENTITY.md](STAFF-IDENTITY.md) 第 5 节（D3 红线） |
| 未映射的 IM 用户会被当群众吗 | **不会**。拒绝 + 留痕，**不回话**——回话等于把机器人变成一个可探测的入口 |
| 重投会重复处理吗 | **不会**。`(channel, message_id)` 主键认领，存储层原子判定 |
| 通知发不出去会怎样 | **只记日志与留痕**。发送在后台线程里，失败不冒泡到流程 |
| 卡片会带业务数据吗 | **不会**。卡片字段只有任务号、状态标签、深链（见第 5 节） |

## 2. 为什么只接受**加密模式**回调

企业微信的回调有两种模式，两者的签名覆盖范围不同：

| 模式 | 签名算法 | 签名覆盖消息体吗 |
|---|---|---|
| 明文 | `sha1(sort(token, timestamp, nonce))` | **否** |
| 加密 | `msg_signature = sha1(sort(token, timestamp, nonce, encrypt))` | 是（覆盖密文） |

明文模式下，同一个 `(signature, timestamp, nonce)` 三元组可以被任意重放，并且配上
**任意 body** ——"验签通过"只能证明"这个三元组出现过"，证明不了"这段内容来自企业微信"。
收下它，验收标准里的"验签失败一律拒绝"会全部通过，但那是一个看起来在验签、实际
没验的实现。所以本实现直接拒绝未加密的回调（`400 im_payload_invalid`，原因写明
"请在管理后台开启加密模式"）。

加密模式还要多校验一步：解开后的明文尾部带 `receive_id`（企业的 corpid）。
它必须与配置一致——能解开只说明双方 key 相同，不能说明这个包是发给我们的。

## 3. 入站：四步，顺序不能换

```
验签（含解密）→ 认领 message_id → 解析 IM 身份 → 固定命令
```

1. **验签在最前面**，由 `infrastructure/wecom.py` 完成。失败直接 403，**连去重表都不写**：
   否则任何人都能拿任意 message id 把去重表写满（一次无成本的状态污染）。
   超大请求在 `await request.body()` 之前就按 `Content-Length` 拒绝（413）——
   adapter 里的实际长度检查要等整个 body 读进内存才生效，那时内存已经花掉了。
2. **去重排在身份解析之前**：重投是平台行为，不该每次都读一遍工作人员目录、
   再写一条访问留痕。`INSERT OR IGNORE` 的 `rowcount` 就是判定结果，不先查后写——
   中间的窗口正好是重投进来的时机。
3. **身份解析**走 `ImIdentityResolver`（#63），它自己也写留痕。失败不回话。
4. **命令是固定的**，只有一条：`状态 <task_id>`（别名 `查` / `查询` / `status` / `query`）。
   整条消息必须就是这条命令，多一个字都不认。不做自然语言理解：一旦它能"理解"，
   它就能被诱导去做别的动作。认不出就不回话。

`<task_id>` 的读取还要过一次**授权**：请求人必须在这张任务的流程上有至少一个动作
（受理/复核/处理）。任务不存在与没有授权对外是同一个结果——区分两者等于提供一个
"这个任务号存在吗"的探测接口。

> 为什么只做"查状态"：这是本 Issue 的用户问题——"为了看一眼任务状态要另开浏览器"。
> IM 里做业务操作（确认、提交、补充材料）都被 D3 与"不做第二套任务状态"否掉了。

## 4. 幂等：两条主键

| 键 | 作用 | 落点 |
|---|---|---|
| `(channel, message_id)` | IM 重投只处理一次 | `im_inbound_messages` |
| `(task_id, status, step_id)` | 同一状态只发一张卡 | `im_notification_marks` |

`message_id` 取企业微信的 `MsgId`；事件回调没有 `MsgId`，用内层报文的 sha1 做键——
重投的报文字节相同，摘要也就相同，幂等仍然成立；内容不同的两次事件不会撞在一起。

发卡去重里带上 `step_id`，是因为一张任务可能**多次**进入"待人确认"（多步确认）。
只按状态去重，第二次确认就不会有卡片。

内存后端只在进程内去重：多实例部署必须用 sqlite（同机）或后续的共享后端，
否则同一消息在 A 实例处理过、在 B 实例还会再处理一次。这条写在"已知限制"里。

## 5. 出站：卡片只带状态与深链

`TaskCard` 的字段表**就是**隐私边界：

```python
TaskCard(task_id, status, title, description, url)   # 就这五个
```

`title` 是状态标签（"待人确认""需转人工"……），`description` 是任务号，`url` 是
`{staff_console_base_url}/tasks/{task_id}`。构建卡片时**不读** `task.slots` /
`task.result` / `task.user_ref`——那三样就是"服务对象数据正文"。
标题与描述还会再过一遍既有的 `redact_text`，兜住"将来有人往标签里加业务字段"。

发卡时机是**任务事件触发**，但只在值得发的状态上发：

| 状态 | 发不发 |
|---|---|
| `waiting_for_confirmation` / `handoff_required` / `outcome_unconfirmed` / `completed` / `failed` | 发 |
| `created` / `running` / `waiting_for_user` / `cancelled` | 不发 |

过程状态逐条推送会让工作人员把这个号静音，真正要看的时候反而看不到。
一条流程一路跑到完成，因此只会收到一张"已办结"。

### 5.1 为什么发送在后台线程

同步发送意味着：对方接口慢 3 秒，任务推进就慢 3 秒；对方挂了，流程跟着挂。
通知是**旁路**，不是流程的一部分。所以：

- 事件发布先给在线订阅者（SSE 的延迟不该被 IM 影响），再往有界队列投一个任务号；
- 队列满时**丢卡片**并记日志，绝不阻塞流程；
- 发送失败只写一条 `CardDelivery(outcome=failed, detail=<错误类型>)`，不写状态、不改任务。

留痕里只放错误类型，不放响应正文：对方报文可能回显请求内容。

送达 ≠ 已读 ≠ 已办。卡片上没有"已读回执"，也没有"在 IM 里点一下就算办完"的路径。

## 6. 配置

```bash
# 回调（三项必须同时给，缺一项启动即失败）
export WORKPANDA_WECOM_CALLBACK_TOKEN='...'        # 企业微信后台的回调 Token
export WORKPANDA_WECOM_ENCODING_AES_KEY='...'      # 43 字符的 EncodingAESKey
export WORKPANDA_WECOM_CORP_ID='ww...'             # 企业 ID，同时用于校验 receive_id

# 出站（留空 = 只接入站、不发卡片，投递记 skipped 而不是 failed）
# 三项必须成套：只配 secret 或只配 agentid、或配了凭据却没有站点根，都会启动失败。
export WORKPANDA_WECOM_CORP_SECRET='...'
export WORKPANDA_WECOM_AGENT_ID='1000002'

# 卡片深链的站点根（不带路径）。有出站凭据时它必填：
# 服务端不猜自己的对外域名，猜错的后果是卡片指向一个不存在的站点。
export WORKPANDA_STAFF_CONSOLE_BASE_URL='https://wpa.example.com'
```

| 变量 | 默认 | 说明 |
|---|---|---|
| `WORKPANDA_WECOM_CALLBACK_TOKEN` | 空 | 与后台回调配置一致 |
| `WORKPANDA_WECOM_ENCODING_AES_KEY` | 空 | 43 字符；长度或 base64 非法则启动失败 |
| `WORKPANDA_WECOM_CORP_ID` | 空 | 校验解密后的 `receive_id` |
| `WORKPANDA_WECOM_CORP_SECRET` | 空 | 取 `access_token` 用；与 agentid 同进同出 |
| `WORKPANDA_WECOM_AGENT_ID` | 0 | 应用 agentid；出站必填正整数 |
| `WORKPANDA_WECOM_TIMEOUT_SECONDS` | 5.0 | 单次发送超时 |
| `WORKPANDA_STAFF_CONSOLE_BASE_URL` | 空 | 卡片深链的站点根；有出站凭据时必填 |
| `WORKPANDA_IM_CARD_QUEUE_CAPACITY` | 200 | 出站队列容量，满了丢卡片 |

配置有三条启动期门禁，都是为了把"静默失效"变成"起不来"：

1. **回调三项**全空 = 关闭入口；只配一部分则启动失败——半套配置会表现为
   "接口开着但每条回调都失败"，那种现象会被误读成"对方配错了"；
2. **出站三项**（secret / agentid / 站点根）必须成套：缺任一项都会让卡片永远发不出去，
   而现场只看到"任务在跑、卡片没有"；
3. **只有出站凭据、没有回调配置**也启动失败：网关不会装配，配了发送凭据也发不出卡片。

## 7. 依赖取舍

新增 `cryptography`（Apache-2.0 / BSD-3，Python Cryptographic Authority 维护）：

- 加密模式回调是 AES-256-CBC，标准库不提供；
- 自己实现 AES 不可接受——加密代码里最贵的不是"算对"，是常数时间与侧信道，
  手写的那一份永远缺一次审计；
- 传递依赖可控（Linux/macOS/Windows 都有 wheel），且它是 FastAPI 生态之外
  唯一新增的运行时依赖。

## 8. 已知限制

- **未与真实企业微信租户联调**：验签、解密、发送的实现按官方协议写，并用固定向量与
  mock transport 覆盖；真实租户的 corpid/agentid/回调地址配置验收需在部署环境完成；
- **只支持加密模式**：明文回调被拒绝（理由见第 2 节）。若部署方只能开明文模式，
  需要先补一条带完整性校验的接入方案，而不是把明文放进来；
- **`access_token` 单进程缓存**：多实例各自取一次，企业微信对 token 有调用频率限制，
  实例数上去之后需要共享缓存；
- **去重表与投递留痕不自动清理**：`im_inbound_messages` / `im_card_deliveries` 会持续增长
  （每消息一行 / 每卡片一行）。sqlite 后端需要外部清理任务，或后续按留存期归档；
  [`docs/CAPACITY-RUNBOOK.md`](CAPACITY-RUNBOOK.md) 的容量口径目前不含这两张表；
- **多实例去重依赖共享存储**：memory 后端只在进程内幂等；多实例试点必须用 sqlite
  （同机）并配合 `expected_instances` 门禁；
- **卡片深链指向工作人员任务页**：该页面尚未实现（本仓库只提供接口与卡片）；
  未配出站凭据时不发卡片（记 `skipped/outbound_disabled`），配了凭据则站点根必填；
- **不做群广播、不做第二个渠道、不做多租户**：按 Issue 的非目标执行。

## 9. 验证证据

`pytest tests/test_im_gateway.py`：29 项

- 验签失败 → 403，且身份解析未调用、去重表未写、无投递留痕（一条用例同时断言四件事）；
- 明文回调 → 400；URL 验证必须带签名（坏签名 403）；
- 同一 `MsgId` 两次 → 只发一张卡；8 线程并发重投 → 1 次 REPLIED + 7 次 DUPLICATE；
- sqlite 后端重建容器后同一 `MsgId` 仍判重投（去重落盘）；
- 未映射用户 → 无卡片 + 拒绝留痕带 IM 标识；有映射但无授权 → 无卡片；
- 认不出的指令（含"帮我确认一下 <id>"、"状态 <id> 顺便办了吧"）→ 不发卡；
- 卡片字段表恰为五个字段（结构断言），槽位原文/联系人不出现在卡片文本里；
- 状态标签里塞手机号 → 发出前被掩码（`138****5678`）；
- 发送端全挂：任务照常 WAITING → COMPLETED，投递留痕全为 failed；
- 无收件人 → `skipped/no_recipient`；只接入站 → `skipped/outbound_disabled`；
- 一条流程跑到完成只发一张卡（过程事件不发）；同一 (状态, 步骤) 只发一次；
- `ReadOnlyTasks` 无 `commit`/`save`/`append_event`，模块 AST 里无写任务调用；
- `/api/im` 只有 GET/POST 一对回调入口（白名单断言）；
- 未配置 → 404；半套回调配置 → 启动失败；半套出站配置 / 有出站无站点根 /
  有出站无回调 → 启动失败；站点根带路径 → 启动失败；
- 声明超限的 `Content-Length` → 413，且业务层完全没被调用；
- memory 与 sqlite 后端在同一组认领语义上一致。

`pytest tests/test_wecom_adapter.py`：18 项

- 签名算法固定向量（`sha1("123a")`）；换掉密文后原签名失效；
- 解密轮次校验 `receive_id`、错误密钥、非法 base64/长度；
- DTD/实体声明在解析前被拒；超大 body 被拒；事件无 `MsgId` 时幂等键稳定；
- 出站：textcard 字段集合、token 只取一次、token 过期只重试一次、业务错误码/HTTP 错误/非 JSON
  都变成异常（不允许"发失败"表现成"发成功"）。
