# 工作人员主体与 IM 身份映射

> 范围：Issue #63 的落地说明——第四类主体 `staff`、按流程授予的动作权限、
> `im_identities` 映射表，以及「未映射的 IM 用户一律拒绝并留痕」。
> 结论先行：**工作人员不是 admin 的别名，也不是"更可信的匿名会话"**。
> 它有独立凭据、独立授权来源，跨通道使用一律 401。

## 1. 为什么是第四类主体

系统里原本只有三类主体，各自解决一个明确的问题：

| 主体 | 凭据 | 能做什么 | 不能做什么 |
|---|---|---|---|
| `citizen`（匿名会话） | `X-WorkPanda-Session` | 提出诉求、回答问题、确认 | 任何跨会话访问；不是实名身份 |
| `ops` | `X-WorkPanda-Ops-Token` | 看任务与留痕（`viewer` / `admin` 两档） | 改流程语义 |
| `admin` | `X-WorkPanda-Admin-Token` | 建版本、发布、回滚、停用 | 办理业务 |
| `staff`（本项新增） | `X-WorkPanda-Staff-Token` | 在**被授权的流程**上受理 / 复核 / 处理 | 改流程语义、看运维明细 |

工作人员不能并入 `admin`，原因是权限量级差一个数量级：`admin` 能改「哪一步需要
群众确认」「这一步能调哪个工具」，这两项在 [ADMIN-CONSOLE.md](ADMIN-CONSOLE.md)
第 6 节被标为「高风险，启用前须业务/安全责任人确认」。把办事员塞进 `admin`，
等于每个窗口都能改确认点，而这恰好是这套系统唯一不能被绕过的授权环节。

也不能"从 IM 网关来就按工作人员算"：企业微信同时挂内部员工与外部联系人，
外部联系人的身份完全不受控（见第 4 节）。

## 2. 凭据配置

```bash
# 主体:staff:密钥，多个用逗号分隔；角色只接受 staff
export WORKPANDA_STAFF_TOKENS='service01:staff:<随机串>,service02:staff:<随机串>'
```

规则与 ops / admin 凭据**完全一致**（同一段解析代码）：密钥至少 16 字符、
不同主体禁止共用密钥、同一主体只能有一个角色、主体名只允许字母数字点下划线连字符。
写错即启动失败，不会静默忽略一条配错的凭据。

未配置 `WORKPANDA_STAFF_TOKENS` 时 `/api/staff/*` 整体返回 404，**不接受**其它三类
凭据代用——否则"没配 staff"会静默退化成"用 admin 冒充工作人员"。

凭据只证明"你是谁"；"能对哪些流程做什么"维护在工作人员目录里（第 3 节），
所以调整授权不需要重新发密钥。

## 3. 授权模型：能对哪些流程做什么动作

```python
StaffMember(
    subject="service01",
    display_name="张受理",
    enabled=True,
    grants=(
        StaffGrant(flow_id="demo.residence", actions=frozenset({StaffAction.INTAKE})),
        StaffGrant(flow_id="demo.refund", actions=frozenset({StaffAction.REVIEW})),
    ),
)
```

动作只有三档，对应业务上的三个动作，不预设更细的粒度：

| 动作 | 含义 |
|---|---|
| `intake` | 受理：接住这笔业务并推进到待处理 |
| `review` | 复核：核对已提交的材料/结果 |
| `handle` | 处理：执行该步骤要求的动作 |

`flow_id="*"` 表示"该动作在所有流程上可用"，但它仍然只管动作，不含任何流程编辑
能力——流程语义始终只属于 `admin`。

未开通（目录里没有这个人）与停用（`enabled=false`）都返回 **403
`staff_not_provisioned`**，而不是 401：凭据是真的，要改的是目录，不是换密钥。
401 会让人反复试密钥，把一个配置问题拖成一次排障。

## 4. IM 身份映射

```sql
-- 实际存储在任务库同一文件的 im_identities 表（sqlite 后端）
(channel, im_user_id) → principal_subject, enabled, note, updated_at
```

维护接口（管理侧凭据，`admin` 角色才能写，`viewer` 只读）：

| 方法与路径 | 作用 |
|---|---|
| `GET /api/admin/identities/staff` | 工作人员目录 |
| `PUT /api/admin/identities/staff/{subject}` | 建/改一名工作人员（含授权） |
| `DELETE /api/admin/identities/staff/{subject}` | 删除，并级联删除指向他的 IM 映射 |
| `GET /api/admin/identities/im?channel=` | 映射列表 |
| `PUT /api/admin/identities/im/{channel}/{im_user_id}` | 建/改一条映射 |
| `DELETE /api/admin/identities/im/{channel}/{im_user_id}` | 删除一条映射 |
| `POST /api/admin/identities/im/resolve` | 诊断一次解析（判定与真实入站相同） |
| `GET /api/admin/identities/im/access-log?rejected_only=` | 解析留痕，新的在前 |

要点：

- **主体名、渠道名、IM 用户 ID 只从路径取**，不接受请求体自报——可自报的主体名
  等于没有主体名；
- 映射目标必须是已存在的工作人员（否则 422 `admin_identity_invalid`）：先配人、
  再配映射，避免留下指向空主体的悬空映射；
- **变更立即生效**：解析每次请求都读目录，没有把映射缓存进进程的路径，所以不存在
  "改完要重启"——重启还会顺带丢掉正在跑的任务上下文；
- 渠道名统一转小写（`WeCom` → `wecom`），**IM 用户 ID 区分大小写**：企业微信的
  userid 就是账号名，大小写不同是两个人。

### 4.1 未映射的 IM 用户

行为被明确定义为**拒绝 + 留痕，不降级为匿名会话**：

| 情形 | 结果 | 留痕里的 `reason` |
|---|---|---|
| 没有这条映射 | 拒绝 | `im_identity_unmapped` |
| 映射被停用 | 拒绝 | `im_identity_disabled` |
| 映射指向的主体不存在 | 拒绝 | `im_principal_missing` |
| 映射指向的主体已停用 | 拒绝 | `im_principal_disabled` |

拒绝记录带 `channel` 与 `im_user_id`：只有主体名的话，事后无法区分"同一个人换了
IM 号"和"两个人被拦"。`origin` 区分真实入站（`gateway`）与管理侧诊断
（`admin_probe`），避免诊断请求把真实拒绝记录淹掉。

为什么不做降级：降级之后，"没配好"表现为"能办事但记不到人"，而且不报错。等发现
时已经有一批业务找不到经办人。

## 5. 与后续 issue 的衔接

- **N4（企业微信网关）**：入站只做验签 → 归一化 → 调
  `container.im_resolver.resolve(channel, im_user_id)` → 用返回的 `Principal`
  调**同一套** application service。网关不得自己拼 `TaskService`，也不得在解析
  失败时"先当群众用"；
- **N5（卡片深链确认）**：`Principal(kind=staff)` 不带任何签发确认凭证的能力，
  IM 文本回复仍然不构成授权（D3 红线不变）。

## 6. 验证证据

`pytest tests/test_staff_identity.py`：28 项，覆盖

- 四类凭据交叉使用全部 401（每类一条回归）＋ staff 调 `/api/admin/flows/*` 写接口被拒；
- staff 未开通/停用 → 403（不是 401）、viewer 可读不可写；
- 未映射 IM 用户被拒且留痕含 IM 标识、解析失败不产生任何 `Principal`；
- 映射增删改在**同一容器内**立即生效、停用后拒绝原因切到 `im_identity_disabled`；
- 渠道名归一化与 IM 标识区分大小写；
- `WORKPANDA_STAFF_TOKENS` 非法配置启动即失败（角色写成 admin、密钥过短、跨主体共用）；
- sqlite 后端重建容器后目录/映射/留痕仍在。

未覆盖（属 N4/N5 范围）：真实企业微信回调验签、卡片投递、IM 内的业务操作。
本轮没有开放任何能改动任务状态的 staff 入口——**没有入口的权限，比有入口但靠自觉更可靠**。
