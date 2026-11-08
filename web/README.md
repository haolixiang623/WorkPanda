# 群众侧最小对话页

群众的办理入口。与 `admin-ui/` 同款形态：独立 TypeScript/ESM、`tsc` 构建、产物提交，
**没有打包器、没有 npm 运行时依赖**。只调用 `/api/tasks/*`，不碰管理侧与运维侧的
任何接口和凭据。

接口与状态契约见 [../docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md) 与
[../design/DESIGN.md](../design/DESIGN.md) 第 3.1 节。

## 文件

| 路径 | 用途 |
|---|---|
| `index.html` | 页面骨架与样式（移动优先）；`#connection` / `#notice` / `#app` 三个挂载点 |
| `src/state_view.ts` | 状态投影：把 `TaskView` 变成"这一屏显示什么"。**纯函数，不碰 DOM、不碰时钟** |
| `src/main.ts` | 交互层：HTTP、事件流、重连、渲染 |
| `tsconfig.json` | 编译配置（ES2020 / DOM / strict / noEmitOnError） |
| `dist/state_view.js`、`dist/main.js` | 已提交的编译产物，两个 ESM 模块必须一起部署 |

构建（产物已随仓库提交，仅在改源码后需要重跑）：

```bash
npx -p typescript@5.6.3 tsc --project web/tsconfig.json
```

编译必须零错误才产出文件。源码改了不重新编译会被
`tests/test_web_ui.py::test_compiled_bundle_is_in_sync_with_source` 拦下。

## 服务端托管

`main.py` 把本目录挂到 `/web/`，`/` 做一次 307 跳到 `/web/`。

**默认只在绑定回环地址时挂载**（`WORKPANDA_WEB_UI_ENABLED` + `bind_host` 回环，
或显式 `WORKPANDA_WEB_UI_EXPOSE_PUBLIC=true`）。理由：这个页面会给任何打开它的
人签发会话，而本部署还没有面向公网的认证与 TLS——挂到 `0.0.0.0` 上等于让局域网里
任何一台机器都能新建任务、占用执行预算。要对外服务，先补网关与认证。

页面按同源请求，不需要 CORS。若改动为跨域部署，注意服务端目前没有
`expose_headers`，浏览器读不到 `X-WorkPanda-Session` 响应头（会话就续不上）。

## 状态怎么来

**只有一条路**：`GET /api/tasks/{task_id}` 返回的 `ui_state`。

- SSE 事件（`/api/tasks/{id}/events`）只当"去问一次服务端"的提醒，事件载荷里的
  状态一概不用。这样"漏了一个事件"最多是多刷新一次，不会让页面停在错的结论上；
- `src/state_view.ts` 里没有 `setTimeout` / `Date` / 动画帧，所以"等动画放完就算
  办完了"这类写法在这一层写不出来（有回归）；
- 服务端先上线了新状态而页面还没跟上时，落到「状态待确认」，**不落到**「已完成」。

11 个服务端状态各有各的一屏，文案（`ui_title`/`ui_hint`）由服务端下发，页面原样
显示——多一份措辞就多一次"结果待确认被写成已办结"的机会。

第 12 屏「需要用户操作」不在服务端状态里：它按"服务端在等用户、但没有让用户填表"
（`awaiting_input` + `pending_fields` 为空）派生。服务端的 `USER_ACTION_REQUIRED`
事件是 Issue #80 的范围，届时这一屏改用真实事件，三要素（去哪 / 做什么 / 多久过期）
也由事件给全；在拿到有效期之前，页面只写"以办理页面上的提示为准"，不编倒计时。

## 「我已完成」的边界

- 它走 `POST /api/tasks/{id}/answers`，请求体只有 `values`；
- 它**不会**发 `POST /api/tasks/{id}/confirm`——签发确认凭证只有一个入口，且必须
  显式带上当前步骤号（D3 红线，有回归）；
- 服务端还没给出可提交的字段时按钮不启用并写明原因：点下去必然 422 的按钮比
  "暂时不能点"更让人困惑。

## 凭据与隐私

- 会话令牌、任务号、对话号只放 `sessionStorage`：同一标签页刷新能续上，关掉标签页
  即失效。业务字段（姓名、电话、住址）一律不落任何本地存储，只在内存与页面上；
- 页面从不读 `location`，不把任何字段写进 URL；
- 不 `console.log` 任何内容，不写 `data-*` 业务值（`data-state` 只有状态键）；
- 回归：`test_page_never_reads_url_or_persistent_storage`、`test_bundle_contains_no_credentials`。

与 `admin-ui/` 的区别：管理侧**禁止**任何存储（管理员凭据能改流程的确认语义），
群众侧允许 `sessionStorage` 只存会话，否则刷新一次就换了会话主体，按 `task_id`
恢复必然 404。

## 手工走查

```bash
uvicorn workpanda.main:app --app-dir src --port 8000
# 打开 http://127.0.0.1:8000/ ，输入「帮我做一次自检，主题：链路自检」
```

1. **正常路径**：创建 → 澄清（一次一个字段）→ 确认页（动作、关键字段、影响）→ 确认 → 完成；
2. **刷新/重连**：在等待输入时按 F5，页面按 `task_id` 回到同一屏，输入框不重复提交、
   已完成步骤不丢；断网 5 秒以上再恢复，先出现"连接中断"提示，重连后以服务端状态为准；
3. **取消**：执行前点「取消本次办理」，显示「已取消」，**不显示为成功**；
4. **转人工**：让执行步骤转人工（脚本化执行器或运维处置），页面显示「已转人工」，
   没有确认按钮；
5. **结果未知**：外部结果无法核实时显示「状态待确认」，且**没有**「重新发起」——
   那会引导群众重复提交一笔可能已受理的申请。

页面本身以手工走查为主；能自动化的部分（投影函数、静态托管开合、视图字段是否给全）
在 `tests/test_web_ui.py`，Node 直接加载编译产物做行为回归。

## 不做的事

移动端原生壳、多轮富文本对话历史、附件上传 UI、扫码与验证码交互（都在 P3 范围内）、
把管理侧或运维侧的任何入口搬进群众页。
