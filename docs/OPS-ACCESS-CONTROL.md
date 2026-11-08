# 运维访问控制与传输安全

> 范围：Issue #31 的落地说明——运维接口的角色隔离、凭据轮换、CORS 白名单，以及
> TLS 与绑定地址的门禁和回退。
> 结论先行：**试点实例仍是"本机回环 + 明文"**。代码侧已经具备开启 TLS 与按角色
> 收窄可见范围的能力，但把服务暴露到局域网或公网属于安全边界变更，需单独确认后
> 才做，本文件不默认公网可达。

## 1. 运维角色与可见范围

运维凭据按主体发放，每个主体带一个角色。只有两档，配错的空间小：

| 端点 | `viewer` | `admin` | 说明 |
|---|---|---|---|
| `GET /api/ops/tasks` | 200 | 200 | 任务列表，不含槽位原文 |
| `GET /api/ops/traces/summary` | 200 | 200 | 留痕聚合指标（未确认占比、被拦调用等） |
| `GET /api/ops/tasks/{task_id}` | 403 | 200 | 任务详情 |
| `GET /api/ops/tasks/{task_id}/events` | 403 | 200 | 事件流，含澄清、确认、执行原文 |
| `GET /api/ops/traces` | 403 | 200 | 留痕明细 |
| `GET /api/ops/audit` | 403 | 200 | 审计归档 |
| `GET /api/ops/tool-runtime` | 403 | 200 | 工具线程台账 |
| `GET /api/ops/metrics` | 403 | 200 | 容量基线与部署形态（Issue #53） |

`/api/ops/metrics` 是新增的暴露面，按最小可见范围起步只给 `admin`：响应里带部署
形态配置（后端类型、实例数声明、租约开关），虽不含连接串、凭据或任务正文，仍不
应在未确认前下放给日常值班的 `viewer`。是否下放需业务/安全责任人确认，确认后同步
更新本矩阵与回归测试，见 [CAPACITY-RUNBOOK.md](CAPACITY-RUNBOOK.md) 第 8 节。

划分依据是"数据范围"而不是"操作方便"：明细里带着槽位内容与对外调用的原文，
把它们开放给所有人看，等于把最小可见范围拉平成"全员可读群众数据"。值班只需要
知道"今天有没有未确认飙升"，那是 `viewer` 的活；排障才需要看单条任务的原文。

状态码语义：

- **404**：未配置任何运维凭据，整个 `/api/ops` 按不存在处理，不暴露接口存在性；
- **401**：没有凭据，或凭据不匹配任何主体；
- **403**：凭据有效但角色不够。这里刻意不用 404——已通过鉴权的主体需要区分
  "我没有这个角色"和"这个版本没上这个接口"，否则会反复怀疑是发版问题。

401 的响应体只有 `ops_token_invalid`，不回显期望值、也不提示"该密钥接近 admin"。
密钥比较是逐个比对、不提前返回，避免响应时间随前缀匹配长度变化而泄露信息。

## 2. 凭据配置与轮换

```bash
# 多主体（推荐）：主体:角色:密钥，逗号分隔
export WORKPANDA_OPS_TOKENS='duty01:viewer:<随机串>,oncall01:admin:<随机串>'
```

```bash
# 单主体旧配置，等价于一个 admin 主体，主体名固定为 legacy
export WORKPANDA_OPS_TOKEN='<随机串>'
```

规则（违反即启动失败，不会静默忽略）：

- 密钥至少 16 字符——短口令的搜索空间小到可以在线枚举；
- 同一主体可以有**多个**密钥，这正好是轮换窗口需要的形态；
- 不同主体**禁止**共用同一个密钥——共用之后任何一次事故都无法定位到人；
- 同一主体只能有一个角色，避免权限边界随命中哪条凭据而变；
- 主体名只允许字母、数字、点、下划线、连字符。

轮换（无空窗）：

1. 加一条同主体、新密钥的记录，保留旧记录；
2. 重启服务（配置为启动时读取），调用方逐一切到新密钥；
3. 确认旧密钥的调用方已归零后，删掉旧记录并再次重启。

撤销单个主体只需删掉它的所有记录，不影响其他主体——共享口令做不到这一点。

> 密钥一律通过环境变量或部署侧的密钥渠道注入，不写入仓库、不写进 `.env` 提交，
> 也不回显到日志或运维接口响应里。

## 3. 四侧鉴权不串味

群众侧、运维侧、管理侧、工作人员侧是四套独立凭据体系，互不认账：

| 尝试 | 结果 |
|---|---|
| 群众会话令牌放进 `X-WorkPanda-Ops-Token` / `-Admin-Token` / `-Staff-Token` | 401（不是任何主体的密钥） |
| 运维凭据放进 `X-WorkPanda-Session` / `-Admin-Token` / `-Staff-Token` | 401 |
| 管理凭据放进 `X-WorkPanda-Session` / `-Ops-Token` / `-Staff-Token` | 401 |
| 工作人员凭据放进 `X-WorkPanda-Session` / `-Ops-Token` / `-Admin-Token` | 401 |

各自都走自己的签名/比对逻辑，没有"任一通过即放行"的分支。回归用例见
`tests/test_ops_access.py::test_session_token_cannot_reach_ops_endpoints`
与 `tests/test_staff_identity.py`（每类凭据各一条）。

工作人员（第四类主体）的凭据、授权模型与 IM 身份映射见
[STAFF-IDENTITY.md](STAFF-IDENTITY.md)：它有独立凭据，能对哪些流程做什么动作由
工作人员目录授予，**不继承** `admin` 的流程编辑能力。

需要强调的范围边界：当前会话身份是**匿名签名会话**，`subject` 只是会话标识，
不是实名身份。因此本节证明的是"通道不串味"，不等于"多个真人用户之间已隔离"；
实名认证与真实身份绑定仍待后续实现。


## 4. TLS 与绑定地址

默认 `bind_host=127.0.0.1`、`tls_enabled=false`，也就是不对外暴露。绑定到非回环
地址要同时满足三个条件，缺一个就**启动失败**（`workpanda.server.validate_network_settings`）：

1. `WORKPANDA_BIND_HOST` 不是回环地址；
2. `WORKPANDA_TLS_ENABLED=true`，且证书与私钥文件都存在；
3. `WORKPANDA_ALLOW_NETWORK_BIND=true`，由人显式确认这次暴露。

```bash
export WORKPANDA_BIND_HOST=0.0.0.0
export WORKPANDA_BIND_PORT=8443
export WORKPANDA_TLS_ENABLED=true
export WORKPANDA_TLS_CERTFILE=/etc/workpanda/tls/fullchain.pem
export WORKPANDA_TLS_KEYFILE=/etc/workpanda/tls/privkey.pem
export WORKPANDA_ALLOW_NETWORK_BIND=true
python -m workpanda
```

只开 `tls_enabled` 而不给证书同样启动失败：uvicorn 在缺少证书参数时会退回明文
监听，而这个降级不会给出任何显式错误，只会在抓包里看得见。

### 4.1 回退

回退是配置级的一步操作，改回默认值重启即可：

```bash
WORKPANDA_BIND_HOST=127.0.0.1 WORKPANDA_TLS_ENABLED=false \
WORKPANDA_ALLOW_NETWORK_BIND=false python -m workpanda
```

回退后服务回到"本机回环 + 明文"，`/healthz` 应仍为 200。若回退是因为证书问题，
先确认新证书可用再重新开启，不要把 `allow_network_bind=true` 与明文绑定凑在一起
当作临时方案——那是最容易留下来的一版配置。

### 4.2 尚未覆盖

- 证书签发与续期（ACME 或单位 CA）不在本仓库内，需部署侧解决；
- 反向代理终止 TLS 的形态未做适配验证（届时 `tls_enabled` 应为 false，但
  `allow_network_bind` 仍要求显式确认）；
- HSTS、证书固定、mTLS 均未启用。

## 5. CORS 白名单

`WORKPANDA_CORS_ORIGINS` 为逗号分隔的来源列表，默认空（不装 CORS 中间件，任何
跨域请求都拿不到放行头）。配置**不接受通配符**，也不接受带路径、查询串或片段的
写法，写错即启动失败：

```bash
export WORKPANDA_CORS_ORIGINS='https://ops.example.gov.cn,https://ops-test.example.gov.cn'
```

理由：一旦放行 `*`，任何站点都能替用户把请求发过来，而浏览器随后会把响应交给
那个站点——凭据体系还在，但数据边界没了。`allow_credentials` 与通配符在规范上
本来也互斥，这里选择让它从配置层就不可能出现。

## 6. 验证证据

- `pytest tests/test_ops_access.py`：21 项，覆盖角色可见性差异、旧配置兼容、
  两侧凭据不串味、轮换窗口、配置非法即失败、CORS 通配符拒绝、TLS 与绑定门禁；
- `ruff check .`；
- 全量 `pytest`（`WORKPANDA_STORE_BACKEND=memory`）。

未验证：真实证书下的 TLS 握手、`bind_host` 改为非回环后的真实可达性与回退演练、
Octop 控制台在多角色下的实际渲染。这些都需要一个独立的试点环境与人工确认，
不在本轮自动化范围内。
