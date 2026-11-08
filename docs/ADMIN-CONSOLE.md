# 管理侧 SOP 配置台

> 范围：Issue #51 的落地说明。管理侧能在浏览器里新建流程草稿、配步骤与字段、
> 维护回放用例、跑回放、发布 / 停用 / 回滚，全程不碰源码。
> 结论先行：**接口默认关闭**。没有配置 `WORKPANDA_ADMIN_TOKENS` 时 `/api/admin/flows/*`
> 整体返回 404，不存在可匿名访问的管理入口。

相关代码：`api/admin.py`（路由与鉴权）、`application/admin_flows.py`（受控操作）、
`domain/flow.py`（字段级校验与运行时重载）、`admin-ui/`（独立 TS 前端）、
`tests/test_admin_flows_api.py`（契约与权限回归）。版本库见 #49，回放与发布门禁见
[SOP-REPLAY.md](SOP-REPLAY.md)，运维只读接口见 [OPS-ACCESS-CONTROL.md](OPS-ACCESS-CONTROL.md)。

## 1. 三套凭据互不认账

| 凭据 | 请求头 | 能做什么 |
|---|---|---|
| 群众会话令牌 | `X-WorkPanda-Session` | 建任务、回答、确认、取消 |
| 运维只读凭据 | `X-WorkPanda-Ops-Token` | 看任务与留痕；唯一写口是追加人工备注 |
| 管理侧凭据 | `X-WorkPanda-Admin-Token` | 改流程定义、用例与发布指针 |

管理侧凭据单独发放，格式沿用 `主体:角色:密钥`（逗号分隔），但**不接受运维凭据代用**：
运维 `admin` 能看任务明细，却不该能改"哪一步需要确认、哪一步能调哪个工具"。
配置写错（角色非法、密钥过短、不同主体共用密钥）与运维侧一样在启动时失败，
不静默忽略。状态码语义与运维侧一致：未配置 → 404（不暴露接口存在性）、
凭据无效 → 401、凭据有效但角色不够 → 403。

```bash
export WORKPANDA_ADMIN_TOKENS='duty01:viewer:<随机串>,sop01:admin:<随机串>'
```

管理侧只支持多主体格式，不提供 legacy 单密钥：能改副作用语义的操作必须能定位到
具体主体。凭据通过环境变量或部署侧密钥渠道注入，不写入仓库、不回显到响应体或日志，
也不写进前端构建产物——页面里由运维运行时输入，仅保存在内存变量中随请求头发送。

## 2. 接口与权限矩阵

| 方法 | 路径 | viewer | admin | 说明 |
|---|---|---|---|---|
| GET | `/api/admin/flows` | 200 | 200 | 版本列表 + 发布指针 + 用例数 |
| GET | `/api/admin/flows/{flow_id}/versions/{version}` | 200 | 200 | 版本内容与用例 |
| POST | `/api/admin/flows/{flow_id}/versions/{version}/replay` | 200 | 200 | 跑回放，只读 |
| POST | `/api/admin/flows` | 403 | 201 | 新建草稿版本 |
| POST | `/api/admin/flows/{flow_id}/versions/{version}/cases` | 403 | 201 | 新增回放用例 |
| DELETE | `.../cases/{case_id}` | 403 | 200 | 删除用例 |
| POST | `/api/admin/flows/{flow_id}/publish` | 403 | 200/409 | 发布；`apply=false` 为预演 |
| POST | `/api/admin/flows/{flow_id}/disable` | 403 | 200 | 停用（清空指针） |
| POST | `/api/admin/flows/{flow_id}/rollback` | 403 | 200 | 回滚到指定历史版本 |

草稿提交体没有 `status` 字段：版本内容里的 status 只是作者态标记，服务端固定写成
`draft`，只有发布指针能让流程生效。前端因此无法构造出一个"自带 published"的版本。

发布 / 回滚 / 停用的请求体同样**没有** `published_by`、`disabled_by`、`actor` 一类
字段：署名一律取 `require_admin_role` 返回的 `OpsPrincipal.subject`，请求体里自报的
同名字段被忽略（有回归）。理由与运维侧的人工备注一致——可自报的署名等于没有署名。
三个操作的响应分别带 `published_by`（发布、回滚）与 `disabled_by`（停用），版本列表
`GET /api/admin/flows` 的每一项也带当前指针上的两个署名，供事后核对「谁把 v3 换成了
v4」。署名只记**最近一次**动作；预演（`apply=false`）不移动指针，返回 `null`。

## 3. 关键设计决策

### 3.1 发布后群众侧必须立即命中（本轮最重要的一处）

`FlowRegistry` 是启动时从 `FlowStore` 装载的进程内目录，`TaskService`、`FlowRunner`
与路由器都持有它的引用。管理侧发布只移动库里的指针，如果不同步目录，就会出现
"接口返回已发布、群众侧还在跑旧版本"的虚假成功。

做法是 `FlowRegistry.reload(store)`：**原地整表替换**，保持对象身份不变，所有已建好的
服务立刻看到新状态；整表赋值保证并发读只会看到旧表或新表，不会看到半张表。
历史版本全部保留，在途任务用 `require(flow_id, 原版本)` 继续推进——回滚是日常动作，
不能有连带伤害。回归用例 `test_publish_makes_flow_hittable_by_citizen_side` 直接从群众侧
`POST /api/tasks` 验证命中，不只在存储层断言指针。

### 3.2 发布走门禁，回滚不走

发布统一经 `FlowReleaseService`：该版本有回放用例时，未通过就拦下并返回 409 +
完整报告（含逐 step 观测与总判定），**指针不动**。没有用例时照旧可发，但响应里
`replay_ran=false` 明确写出"没有基线"，不伪装成检查通过。`apply=false` 是预演，
不写任何状态，响应 `published=false`。

回滚恢复的是一个已经发布过的状态，不是新版本准入。若回滚也卡在回放门禁上，
用例被改动或误删时运维就无法快速退回，反而放大事故。因此回滚只校验目标版本确实
存在，再移动指针；门禁管的是新版本准入。这个取舍需要在正式试点前由业务与安全
责任人确认（见第 6 节）。

### 3.3 错误定位到具体字段

pydantic 先挡字段形状（422 带 `loc`），再由 `flow_field_errors()` 挡语义
（execute 必须声明 tool、非执行步骤不得带 tool / 要求确认、step_id 不重复），
错误以 `fields: [{loc, message}]` 返回。管理页按 `data-loc` 把错误标到对应输入框。
`validate_flow()` 与字段级校验共用同一套规则，存储层写入时仍会再兜一次底。

### 3.4 报告要带总判定

`ReplayReport.verdict` / `publishable` 是 property，不进 `model_dump()`。管理侧用
`serialize_report()` 在服务端补齐这两个字段：否则前端只能从逐条结果自行重算门禁口径，
迟早和后端对不上。

### 3.5 发布署名与幂等迁移（#61）

`FlowStore.publish(flow_id, version, *, actor)` 与 `disable(flow_id, *, actor)` 把
`actor` 定为**仅关键字必填**参数：漏传是类型错误，不会静默记成空串。`flow_releases`
在既有三列之外增加 `published_by` / `disabled_by`；`SqliteFlowStore` 启动时按
`PRAGMA table_info` 判断并 `ALTER TABLE ... ADD COLUMN`，迁移可重复执行，旧行的两列为
`NULL`（表示迁移前发生的操作没有署名），读取侧不猜、不回填。

停用不再 `DELETE` 那一行——删掉会把 `disabled_by` 一起抹掉。改为在这行上写停用标记，
`published_versions()` 过滤带标记的行，「没有指针」的对外语义不变；重新发布时清掉
停用标记，避免界面同时显示两个互相矛盾的责任人。版本内容一行都不删，回滚就是
一次 `publish`，所以 `published_by` 一并覆盖「谁回滚的」。

装配期播种（`seed_demo_flows`）是系统动作，署名固定为 `bootstrap`，明显不是人员主体；
CLI `scripts/replay_flow.py --publish` 必须显式给 `--actor`。本轮**不做双人复核，也不
预留 `reviewed_by`**：留一个永远为空的字段会让人误以为环节存在但没人用。

## 4. 验收标准对照

| 验收项 | 落地与回归 |
|---|---|
| 建草稿 → 发布 → 群众侧命中 | `test_publish_makes_flow_hittable_by_citizen_side` |
| 缺参/歧义：前后端都拒绝并定位字段 | `test_draft_rejects_invalid_field_shapes_with_location`、`test_draft_rejects_semantically_illegal_flows_with_field_location` |
| 失败/超时：明确报错、不出现"看起来已发布" | `test_dry_run_does_not_move_publish_pointer`、`test_release_gate_blocks_publish_without_moving_pointer` |
| 取消/重试/恢复：回滚立即生效、在途任务不打断 | `test_disable_stops_new_citizen_tasks`、`test_rollback_restores_old_version_without_breaking_inflight_tasks` |
| 不应发生：前端构造出后端不允许的流程 | 字段级校验 + `test_status_is_authoring_only_and_versions_are_immutable` |
| 不应发生：viewer 发布或回滚 | `test_viewer_can_read_but_cannot_change_anything` |
| 不应发生：管理侧凭据泄漏 | `test_admin_credential_never_appears_in_responses`、`test_admin_ui_assets_do_not_embed_credentials`、三套凭据互不认账用例 |
| 契约测试：合法/非法各若干 | 4 组形状 + 4 组语义 + 版本不可变 + 用例增删 |
| 手工走查 | 运维视角「建草稿 → 回放 → 发布 → 回滚」仍需在试点浏览器里走一遍，见第 6 节 |

## 5. 部署与运维

- 前端在 `admin-ui/`，与群众侧分开构建、分开部署；静态托管 `index.html` 与
  `dist/main.js` 即可，`tsc --project admin-ui/tsconfig.json` 可重新生成产物。
- 若管理页与服务端不同源，把管理页来源加入 `WORKPANDA_CORS_ORIGINS` 白名单
  （不接受通配符）。服务端默认仍只监听回环且不开 TLS；对外暴露需同时满足
  `bind_host` 非回环、`tls_enabled=true` + 证书存在、`allow_network_bind=true`，
  见 [OPS-ACCESS-CONTROL.md](OPS-ACCESS-CONTROL.md) 第 4 节。
- 轮换与撤销沿用运维侧流程：加新密钥 → 重启 → 调用方切换 → 删旧密钥 → 重启。
- 操作顺序建议：建草稿 → 加用例 → 回放 → 预演发布 → 发布 → 需要时回滚。
  发布结果以响应的 `published` 字段为准，界面不会自行推断成功。

## 6. 影响面与已知限制（高风险，启用前须确认）

本 Issue 风险等级为高：管理侧能改"哪一步需要确认、哪一步能调哪个工具"，等于能改
副作用语义与权限边界。以下事项**没有在本轮解决**，启用前需业务 / 安全责任人确认：

1. **默认关闭**：未配置管理凭据时接口整体 404；是否启用、由哪些主体持有 admin
   凭据属于部署决策，不在代码里代做决定。
2. **单实例语义**：`reload()` 只更新当前进程。多实例部署时其它实例仍持旧目录，
   需要重启或由 #53（容量基线 / 多实例）定义发布同步机制；在此之前不要在多实例上
   依赖"发布立即全局生效"。
3. **回滚绕过门禁**：3.2 的取舍需责任人确认；若要求回滚也过门禁，应另做设计评审，
   并给出用例被误删时的应急通道。
4. **意图样例由版本内容携带，#52 的发布门禁会拦**：`intent_samples` 是
   `FlowDefinition` 的字段，API 建草稿时随请求体一起提交；但本轮表单没有样例录入项，
   所以**只用界面建出来的流程没有样例，发布会返回 409**（`admin_intent_gate_blocked`，
   报告里点名缺哪一类、缺几条）。补齐样例需要走 API/脚本，规则本身仍由装配期代码给出，
   群众说一句话自动路由到新流程属于后续事项。当前无样例的新流程可由客户端显式
   指定 `flow_id` 命中，或由部署侧补规则。
5. **独立域名 / 端口 / 证书**：待部署形态确定（见 #53），本轮不决定。
6. **人工走查未完成**：自动化覆盖的是接口契约与权限；真实浏览器里完成一次
   「建草稿 → 回放 → 发布 → 回滚」并核对群众侧表现，仍需在试点由人执行。
7. **多级审批、多人协作与评论**：Issue 明确列为非目标，本轮未实现。

## 7. 验证证据

- `ruff check .` 全过。
- 全量 `pytest`（`WORKPANDA_STORE_BACKEND=memory`、`--basetemp` 在 OS 临时目录）通过；
  新增 `tests/test_admin_flows_api.py` 22 项覆盖第 4 节表格。
- 远端两项 CI（Python 检查、文档与敏感信息基础检查）在 PR head 与 main 均为 success。
- 前端 `tsc --strict --noEmitOnError` 编译通过，产物 `admin-ui/dist/main.js` 已提交。
- 发布署名（#61）：`tests/test_flow_store.py` 覆盖 actor 仅关键字必填 / 非空、发布与
  回滚署名、停用与重新发布、旧库幂等迁移（旧数据为 `NULL`）、内存实现同语义；
  `tests/test_admin_flows_api.py` 覆盖请求体伪造 `published_by` 无效、viewer 不能靠
  伪造署名提权、预演不产生发布人。
- 本轮环境未安装 `tsc`，`admin-ui/dist/main.js` 按 `src/main.ts` 逐处手工同步，
  **未做 TypeScript 类型编译验证**；后续在装有 TypeScript 的环境应复跑
  `tsc --project admin-ui/tsconfig.json` 并核对产物无差异。

## 8. 复盘

1. **最费时的环节**：确认"发布指针移动"与"群众侧实际命中"之间的一致性。存储层
   测试全绿，但那只能证明指针动了。
2. **根因**：`FlowRegistry` 是启动时的进程内快照，发布改的是库里的另一份状态；
   两个真相各自都对，合起来才是错的。
3. **既有测试为何漏掉**：#49 / #50 的回归都在存储层与服务层断言指针与报告，
   没有一条从群众侧建任务去验证"发布后能不能跑"。
4. **最小持久预防**：`FlowRegistry.reload()`（原地、保身份）+ 端到端回归
   `test_publish_makes_flow_hittable_by_citizen_side` 与回滚用例；凡是"改了配置就该
   立即生效"的功能，都要求一条从真实入口发起的回归，而不只是存储层断言。
5. **后续**：多实例发布同步由 #53 承接，可观察完成标准 = 多实例下发布后所有实例
   在约定时限内一致且可监测；意图样例的录入界面与规则装配仍待补（#52 已交付
   "样例随版本 + 发布门禁"，表单尚未覆盖样例录入，见第 6 节第 4 条）。
