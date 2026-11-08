# 企业微信 IM 网关

> 范围：Issue #64 的落地说明——入站回调（验签 + 按 message id 幂等 + 归一化）、
> 身份解析走 Issue #63 的映射表、出站卡片（只含状态与一个深链）、投递失败
> fail-open。依赖已完成的 #63 主体模型。
>
> 一句话结论：**网关是通道，不是执行者，也不是权限来源。**

## 1. 它放在哪里，不做哪些事

| 层 | 模块 | 职责 |
|---|---|---|
| 契约 | `domain/im_gateway.py` | 验签算法、回放闸、入站消息模型、卡片内容边界 |
| 应用 | `application/im_gateway.py` | 幂等入站处理（`ImInboundService`）、任务事件 → 卡片（`TaskEventNotifier`） |
| 适配 | `infrastructure/wecom_gateway.py` | 企微报文解析、access_token、卡片投递、有界投递队列 |
| 接口 | `api/im.py` | `GET/POST /api/im/wecom/callback` |

**不做**（与本轮决策一致）：不在 IM 里做业务操作（D3）、不做群机器人广播、
不接第二个渠道、不做 IM 侧的第二套任务状态（送达 ≠ 已读 ≠ 已办）。

关于「网关不得自行驱动流程」这条边界，代码层面是可审的：
`ImInboundService` 的构造参数只有 `resolver` 与 `messages`，**没有 `TaskStore`、
没有 `TaskService`、没有 `ConfirmationStore`**。因此"网关顺手改一下任务"不是靠
自觉避免，而是不存在那条依赖。`tests/test_im_gateway.py` 里有一条源码审计回归
钉住这一点。

## 2. 入站：先验签，再碰请求体

```
POST /api/im/wecom/callback?signature=&timestamp=&nonce=
  1. 签名校验（含时间窗）        → 失败 403，且不解析 body
  2. nonce 回放闸（一次性）      → 失败 403
  3. 报文体积与 DTD 检查         → 失败 403
  4. XML 解析 → 归一化           → 格式不合法 403
  5. 按 message id 判重          → 重复则不再处理
  6. 身份解析（#63 的映射表）    → 未映射/停用：留痕后拒绝
  7. 留痕并返回 success
```

顺序不是风格问题。先解析再验签，等于把 XML 解析器、正则和日志都暴露给匿名输入；
先解析身份再判重，会让重投把真实拒绝记录淹掉。

### 2.1 只支持明文模式，安全模式启动即拒绝

企业微信回调有两种模式：

- **明文模式**（本轮实现）：只用 `Token`。签名是
  `sha1(sort(token, timestamp, nonce[, echostr]))`。
- **安全模式**（未实现）：消息体用 AES-CBC 加密，需要按 `EncodingAESKey` 解密，
  也就是需要引入加密依赖。

配置了 `WORKPANDA_WECOM_ENCODING_AES_KEY` 时**启动直接失败**并说明原因。这不是
偷懒：静默降级的表现是"回调收得到、消息永远是乱的"，比明确拒绝难查得多。

### 2.2 明文模式的已知弱点与补强

明文模式的签名**不覆盖消息体**：截获一对 `(signature, timestamp, nonce)` 后替换
XML 重发是可行的。补强有三道：

1. **时间窗**：默认 300 秒（`WORKPANDA_WECOM_CALLBACK_MAX_SKEW_SECONDS`），超出即拒；
2. **nonce 一次性**：同一个 nonce 第二次使用直接 403。企微每次推送都用新 nonce，
   对正常流量无感，但它正好卡住"换 body 重发"这个动作；
3. **HTTPS + Token 不外泄**：这是明文模式的真实安全上限。回调地址必须走 TLS，
   Token 不得写进仓库、示例、日志或工单。

仍未覆盖的（后续项，不假装解决）：nonce 记忆是**进程内**的，多实例部署时同一 nonce
的第二次请求落到另一实例仍会通过。跨实例回放防护要求签名覆盖消息体，属安全模式的
能力范围。

### 2.3 幂等

`im_inbound_messages` 表以 `(channel, message_id)` 为主键，用
`INSERT ... ON CONFLICT DO NOTHING` 的 `rowcount` 判定"我是不是第一个"。判重必须由
存储裁决：应用层"先查再写"中间那个窗口，在重投密集时一定会被踩到。

事件类推送（订阅、菜单点击）没有 `MsgId`，用 `发件人:CreateTime:Event` 合成稳定
标识——不合成的话，同一事件的每次重投都会被当成新消息。

内存后端重启即丢，所以重启后可能把一条重投再处理一次。这一层**不产生任何业务
副作用**（只判重与留痕），因此代价只是多一条留痕；要跨重启判重就用 sqlite 后端。

### 2.4 身份解析

完全复用 #63 的 `ImIdentityResolver`：`(channel, im_user_id) → Principal(kind=staff)`。
未映射、映射停用、主体缺失、主体停用四种情形**一律拒绝并留痕**，不降级为匿名会话。
拒绝记录带 `channel` 与 `im_user_id`，与 `GET /api/admin/identities/im/access-log`
看到的是同一份数据（`origin=gateway`）。

## 3. 出站：任务事件 → 卡片

### 3.1 哪些事件会推

`CARD_EVENTS`：待确认、转人工、结果待确认、完成、失败、取消。

步骤开始/完成这类过程事件**不推**——那是把内部节奏外化成骚扰。

### 3.2 推给谁

该流程上有**任一动作授权**、且配置了有效 IM 映射的在职工作人员。不是群广播，也不按
角色或群成员名单：没有授权的人收到卡片也办不了事，却多知道了一笔业务在发生。

### 3.3 卡片里有什么

标题（状态文案）、一句流程标题 + 下一步提示、一条深链
`{base}/staff/tasks/{task_id}`。

`build_task_card` 的入参是**标量**（task_id / flow_id / flow_title / status），
**不接收 `Task`**。这是本模块最关键的一处签名：只要入参里没有 `Task`，槽位、办理
结果、会话主体、材料正文就没有进入卡片的通路。用 `Task` 当入参的实现，迟早有人
"顺手"把摘要加进去，而那种改动在评审里看起来完全无害。

深链基址（`WORKPANDA_NOTIFICATION_DEEP_LINK_BASE`）必须是绝对 http(s) 源，不带
凭据、路径、查询串。配置错时宁可这张卡发不出去——把卡片指向一个带账号的地址、或
指向一个只在内网可达的主机，等于把内部拓扑发到 IM 里。

### 3.4 通知失败 = 什么也不做（fail-open）

卡片是附带通知，它的成本（网络往返、对方接口的响应时间）不能决定业务路径的进度：

- `NotifyingEventPublisher` **先推送 SSE、再排队**投递卡片，任何时候都不抛错；
- 投递在有界队列 + 单个后台线程里做，任务线程不等；
- 队列满就丢并计数（`dropped`），绝不阻塞推进；
- 失败只记一行日志（异常类型名，不带对方返回体）+ 计数；
- `TaskService` 不感知通知的存在：**任务状态不因通知失败而改变**。

异常消息不外泄的原因很具体：对方返回体、含 `access_token` 的 URL 都可能出现在
异常里，落进日志等于二次泄漏。

### 3.5 不重试

发消息不是幂等操作。对方明确返回失败时重试没有意义；网络中断时重试可能让同一件事
打扰两遍，而"重复打扰工作人员"正是这套通知最容易被投诉的地方。失败就记日志，下一次
状态变化自然会再推一张。

## 4. 配置

```bash
# 入站（必需）
export WORKPANDA_IM_GATEWAY_ENABLED=true
export WORKPANDA_WECOM_CALLBACK_TOKEN='<企业微信回调 Token，至少随机 32 位>'
export WORKPANDA_WECOM_CALLBACK_MAX_SKEW_SECONDS=300

# 出站（四项齐备或全部留空；只配一部分启动即失败）
export WORKPANDA_WECOM_CORP_ID='ww...'
export WORKPANDA_WECOM_AGENT_ID='1000002'
export WORKPANDA_WECOM_APP_SECRET='<应用 Secret>'
export WORKPANDA_NOTIFICATION_DEEP_LINK_BASE='https://service.example.cn'
```

规则：

- `im_gateway_enabled=false`（默认）时 `/api/im/*` 整体 **404**，不暴露接口存在性；
- 开启但没配回调 Token → 启动失败。**不存在"跳过验签"**：那等于给所有未配置的
  部署开一个匿名入口；
- 出站四项只配一半 → 启动失败。否则表现为"配置看起来对、卡片永远不到"；
- `WORKPANDA_WECOM_ENCODING_AES_KEY` 一旦非空 → 启动失败（安全模式未实现）。

回调地址配在企微管理后台：`https://<域名>/api/im/wecom/callback`。

## 5. 运维怎么自查

| 想确认 | 看哪里 |
|---|---|
| 接口是否开着 | `GET /api/im/wecom/callback` 无参数：未开启 404，开启且验签失败 403 |
| 消息有没有到达 | `im_inbound_messages` 表（sqlite 后端）或 `ImInboundService.get()` |
| 谁被拦了、为什么 | `GET /api/admin/identities/im/access-log?rejected_only=true` |
| 卡片发出几张、失败几张 | `NotificationCounters.snapshot()` → `delivered / failed / dropped / handler_errors` |

`NotificationCounters` 由 `NotificationQueue`（记丢件、处理异常）与
`TaskEventNotifier`（记每张卡的投递结果）**共用同一个对象**：

- `delivered`：成功投出的卡片数；
- `failed`：投递失败的卡片数；
- `dropped`：队列满被丢弃的事件数；
- `handler_errors`：处理事件本身抛异常的次数。

两者拆成不同的计数对象，"丢了几张"和"发了几张"就永远对不上账，而"通知体系是否
正常"恰恰要靠这两个数字一起看。

计数目前只在进程内，尚未挂到 `/api/ops/metrics`；这是有意留的缺口，避免为了好看把
一个"没人在看的指标"做成接口（后续接运维面板时再一起做）。

## 6. 验证证据

`pytest tests/test_im_gateway.py` 覆盖：

- 重投同一 message id 只处理一次（内存后端与 sqlite 后端各一条，且第二次不重复留痕）；
- 验签失败（签名错、时间窗超限、缺参数、未配置 token）一律 403，且**未映射标记的
  报文未被解析**（不产生任何入站记录）；
- 同一 nonce 第二次请求 403；
- DTD/ENTITY 报文、超长报文、非 XML 报文一律 403 且不落痕；
- 未映射 IM 用户：接口 200（不回 403，避免重投风暴），但留痕为拒绝且带 IM 标识；
- 卡片不含槽位原文：构造入口取标量，回归断言卡片文本里没有槽位取值；
- 通知失败不影响任务推进：投递器永远失败时任务照常到达 `completed`，失败只进计数；
- 投递失败只留错误码：`wecom_errcode_*` / `http_status_*` / `response_not_json`，
  对方返回体、含 token 的 URL 都不外传；
- 队列满时丢事件并计数，调用方不阻塞；
- 网关不持有写业务状态的能力：`ImInboundService` 构造参数审计 + 源码审计。

未覆盖（属 N5 范围）：卡片深链的确认凭证签发、IM 文本不得签发凭证的红线回归。
