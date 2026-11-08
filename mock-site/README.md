# 本地模拟三方站（mock-site）

> **这是模拟站。** 它不连接、也不代表任何真实政务系统。页面上的账号、事项、回执号
> 全部是本地编造的数据，任何行为都不代表真实政务系统；请不要往里填真实个人信息。

一个独立的 FastAPI 应用，两个端口、两个 origin，用来当 P2（浏览器执行器）、P3（人机协同）
和 P4（试点流程）的靶子。靶子不稳，测出来的是靶子的问题，不是产品的问题。

## 为什么必须真的跨 origin

`/services → /apply` 走的是**另一个端口**（`127.0.0.1:8102`），不是同一个应用里的路径跳转。
这不是为了折腾：域名白名单、每任务一个 browser context、cookie 作用域这些真问题，只有在
真的换 origin 时才会暴露。顺手记一条容易被忽略的事实——**cookie 不按端口隔离**，
`127.0.0.1:8101` 种下的 cookie 会照样带去 `127.0.0.1:8102`；跨站交接因此不能靠 cookie，
只能靠一次性票据（见下）。

## 快速开始

```bash
cd mock-site
python -m mock_site                     # 账号站 8101，办理站 8102
python -m mock_site --check             # 只校验配置与端口占用
python -m mock_site --submit-mode error_but_accepted
```

演示账号 `demo / demo1234`。站点只监听回环地址，绑定别的地址会直接启动失败。

依赖：只复用仓库已有的 `fastapi` / `uvicorn` / `httpx`，没有新增第三方包。multipart 解析
用自带实现（标准库），所以不需要 `python-multipart`。

## 会话与跨站交接

1. 账号站登录成功后下发 `mock_session`（HttpOnly、SameSite=Lax）；
2. 点事项的「去办理」→ `GET /handoff/{service_id}` 签发**一次性**票据（默认 120 秒有效），
   303 跳到 `http://127.0.0.1:8102/apply?ticket=...`；
3. 办理站兑换票据，换成自己的 `mock_apply_session`，再 303 到 `/apply/tab1`，票据从 URL 上消失；
4. 三个 tab 之间靠服务端 session 保持状态，不依赖前端暂存，也不依赖 tab 之间共享内存。

票据只能兑换一次（第二次 `409 handoff_used`），过期 `410 handoff_expired`。

## 页面与定位点

所有可操作元素都带 `data-testid`。换真实站点时，这些定位点会第一个碎——这正是要暴露的课。

| 路径 | 内容 | 关键 `data-testid` |
|---|---|---|
| `/login` | 账号 + 密码 + 图形验证码 | `login-form` `input-username` `input-password` `input-captcha` `captcha-image` `login-submit` |
| `/login/qr` | 扫码登录：占位二维码 + 轮询状态 + 90 秒过期 | `qr-image` `qr-ticket` `qr-status` |
| `/services` | 5 个事项，其中 1 个是试点事项 | `service-item` `service-pilot` `service-start` |
| `/apply/tab1` | 户籍类型（下拉）、办理情形（多选）、电话、地址 | `field-domicile` `field-situation-first|change|reissue` `field-phone` `field-address` |
| `/apply/tab2` | 多附件上传（2-5 个，限类型与大小），已上传可删 | `upload-input` `upload-submit` `attachment-item` `attachment-count` `attachment-delete` |
| `/apply/tab3` | 摘要 + 提交；缺项时列出阻塞项并禁用按钮 | `summary-*` `submit-blockers` `submit-application` |
| `/receipt/{no}` | 回执页（浏览器看） | `receipt-no` `receipt-status` |
| `/receipt/{no}/status` | 回执 JSON（执行器回读核验用） | — |

回执回读是刻意的设计：**不要凭「提交按钮点过了」判断成功**。提交后拿到回执号，再用
`/receipt/{no}/status` 或 `Accept: application/json` 读一次，才算确认。

## 五个不稳定开关

全部可通过 `MOCK_SITE_*` 环境变量配置，也可以在运行期用 `POST /__mock__/config` 改
（只接受回环调用），后者是为了让同一个进程里先跑正常路径、再翻成失败路径。

| 开关 | 默认 | 怎么触发 | 预期的表现 |
|---|---|---|---|
| 验证码错误锁定 | `MOCK_SITE_CAPTCHA_MAX_FAILURES=3` | 连续 3 次验证码或密码错误 | 第 4 次起 `423 account_locked`，正确密码也拒绝；不自动解锁，用 `POST /__mock__/reset` |
| 二维码过期 | `MOCK_SITE_QR_TTL_SECONDS=90` | 90 秒内不确认 | 状态 `expired`，确认 `410 qr_expired` |
| 上传超限 | `MOCK_SITE_MAX_UPLOAD_BYTES=262144`、`MOCK_SITE_MAX_ATTACHMENTS=5` | 单个文件超限 / 超过 5 个 | `413` / `409`；同一批里合格的文件仍然保留（部分成功是刻意的） |
| 提交后不返回 | `MOCK_SITE_SUBMIT_MODE=no_response`，`MOCK_SITE_SUBMIT_HANG_SECONDS=30` | 提交 | 调用方读超时，服务端最终 `504`，且**没有回执**——重试不会造成重复受理 |
| 500 但已受理 | `MOCK_SITE_SUBMIT_MODE=error_but_accepted` | 提交 | 返回 `500`，但回执已经生成；响应里不含回执号，只能靠回读发现 |

「慢响应」是同一个开关里的正常模式：`MOCK_SITE_SUBMIT_MODE=slow` 搭配
`MOCK_SITE_SUBMIT_DELAY_SECONDS`。注意它和 `no_response` 是**两种不同的未知**：慢模式下
调用方若提前放弃，回执仍然会生成（已受理但调用方不知道）；`no_response` 则明确不落回执
（未受理）。执行器要能区分这两种，而不是一律当失败。

## 仅测试用的端点

`/__mock__/*` 只接受回环地址调用（非回环 `403 loopback_only`），错误一律回 JSON。

| 端点 | 用途 |
|---|---|
| `GET /__mock__/state` | 会话、票据、回执数量与当前开关 |
| `POST /__mock__/config` | 运行期改开关（白名单校验） |
| `POST /__mock__/reset` | 清空全部状态并把开关恢复成启动值 |
| `GET /__mock__/captcha/{id}` | 读验证码答案（页面上不暴露文本） |
| `POST /__mock__/qr/{ticket}/confirm` | 模拟「手机端确认」 |
| `GET /__mock__/receipts` | 当前会话的回执列表，用来证明「响应说 500，服务端其实已受理」 |

## 边界（由测试守住）

- **不 import 产品代码**：`mock_site/*.py` 里出现 `workpanda` 直接测试失败；
- **只允许标准库 + fastapi/starlette/uvicorn**：多一个依赖就红，避免靶子和产品被绑在一起；
- **零外链**：页面没有 CDN、字体文件、埋点；FastAPI 的默认文档页也关掉了，因为它会从 CDN 拉
  swagger-ui；
- **只监听回环**：`bind_host` 不是回环地址就启动失败；
- **两份 app 可分别构造**：`create_account_app()` 与 `create_apply_app()` 各自独立，
  测试里既有 ASGI 接口级覆盖，也有真起两个 uvicorn 端口的全链路。

## 手工走查（浏览器）

```bash
cd mock-site && python -m mock_site
```

1. 打开 `http://127.0.0.1:8101/login`，看 `captcha-image` 与 `mock-banner`；
2. 用 `demo / demo1234` + 验证码登录，应跳到 `/services`，看到 5 个事项、其中 1 个带「试点」；
3. 点试点事项的「去办理」，地址栏应变成 `127.0.0.1:8102/apply/tab1`；
4. tab1 填户籍类型、勾两项办理情形、填 11 位手机号与地址，保存后到 tab2；
5. tab2 选 2 个 PDF/PNG 上传，列表出现 2 项；删掉 1 个再补上；
6. tab3 看摘要与材料数量，提交；
7. 落到 `/receipt/{no}`，看到回执号与 `accepted`；再访问 `/receipt/{no}/status` 看 JSON；
8. 故意输错验证码 3 次，确认第 4 次被锁定（`login-locked`）。

## 测试

```bash
pytest mock-site/tests        # 只跑模拟站
pytest                        # 全量，模拟站也在里面
ruff check .
```

`pyproject.toml` 的 `pythonpath` 已包含 `mock-site`，所以不需要设置 `PYTHONPATH`。

## 已知限制

- 二维码是**占位图案**，不是可扫描的编码；票据文本在 `qr-ticket` 上，执行器读文本而不是识图；
- 状态全在内存，重启即清空；两个 app 必须同进程（默认就是这样），跨进程不共享票据；
- 提交**不查重**：同一草稿提交两次就是两张回执。这是刻意的——重复请求会不会重复产生副作用，
  是产品要证明的事，靶子不替它兜底；
- 锁定不按时间自动解锁，只能用 `/__mock__/reset`，避免测试等待；
- multipart 解析器只认浏览器实际会发的形状，不支持嵌套 multipart 与流式超大文件；
- 不做真实身份核验，不接短信、对象存储、实名通道。
