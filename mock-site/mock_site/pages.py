"""模拟站的 HTML：手写字符串，不引模板引擎。

为什么不引 Jinja2：模拟站的全部页面加起来不过几百行 HTML，多一个依赖就多一条
「模拟站和产品一起升级」的维护线。这里只有 `escape()` 和 f-string，改起来是肉眼
可见的；页面也不追美观，追的是**每个控件都有稳定的 `data-testid`**。

页面里没有任何外部资源：没有 CDN、没有字体文件、没有埋点。样式和脚本都内联，
这样「不联网」不是一句承诺，而是打开源码就能验证的事实。
"""

from __future__ import annotations

from collections.abc import Sequence
from html import escape

from mock_site.config import MockConfig
from mock_site.state import (
    DOMICILE_TYPES,
    SITUATIONS,
    ApplySession,
    Attachment,
    Receipt,
    Service,
)

STYLE = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body {
  margin: 0; color: #1f2328; background: #f4f5f7;
  font: 15px/1.6 -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
}
header.mock-banner {
  background: #fff4d6; border-bottom: 1px solid #e6c86e; color: #6b4d00;
  padding: 8px 16px; font-size: 13px; text-align: center;
}
main { max-width: 760px; margin: 0 auto; padding: 20px 16px 48px; }
h1 { font-size: 20px; margin: 0 0 12px; }
h2 { font-size: 16px; margin: 0 0 4px; }
.card { background: #fff; border: 1px solid #dfe3e8; border-radius: 8px; padding: 16px;
        margin-bottom: 16px; }
label { display: block; margin: 10px 0; font-weight: 600; }
label.inline { display: inline-flex; gap: 6px; font-weight: 400; margin-right: 18px; }
input[type=text], input[type=password], select {
  width: 100%; padding: 8px; margin-top: 4px;
  border: 1px solid #c9ced6; border-radius: 6px; font: inherit;
}
fieldset { border: 1px solid #dfe3e8; border-radius: 6px; margin: 12px 0; padding: 8px 12px; }
legend { font-weight: 600; padding: 0 4px; }
button { background: #1f6feb; color: #fff; border: 0; border-radius: 6px;
         padding: 8px 16px; font: inherit; cursor: pointer; }
button.secondary { background: #eef1f5; color: #1f2328; }
a.button { display: inline-block; background: #1f6feb; color: #fff; text-decoration: none;
           padding: 6px 12px; border-radius: 6px; }
.error { background: #fff0f0; border: 1px solid #f0b4b4; color: #a11;
         padding: 8px 12px; border-radius: 6px; }
.field-error { color: #a11; font-size: 13px; margin: 2px 0 0; font-weight: 400; }
.hint { color: #5a6472; font-size: 13px; }
.captcha-row { display: flex; gap: 12px; align-items: center; }
.captcha-row img { border: 1px solid #c9ced6; border-radius: 4px; }
.tabs { display: flex; gap: 8px; margin-bottom: 16px; }
.tabs a { flex: 1; text-align: center; padding: 8px; border-radius: 6px;
          text-decoration: none; background: #fff; border: 1px solid #dfe3e8;
          color: #1f2328; }
.tabs a[data-state=current] { border-color: #1f6feb; color: #1f6feb; font-weight: 600; }
.tabs a[data-state=done]::after { content: " \\2713"; color: #1a7f37; }
ul.services { list-style: none; padding: 0; margin: 0; }
li.service { border-bottom: 1px solid #eef0f3; padding: 12px 0; }
li.service:last-child { border-bottom: 0; }
.tag { background: #e7f0ff; color: #1f6feb; border-radius: 4px; padding: 1px 6px;
       font-size: 12px; }
table.summary { width: 100%; border-collapse: collapse; }
table.summary th, table.summary td { text-align: left; padding: 6px 8px;
        border-bottom: 1px solid #eef0f3; font-weight: 400; }
table.summary th { width: 34%; color: #5a6472; }
code.receipt { font-size: 18px; font-weight: 700; letter-spacing: 1px; }
ul.plain { margin: 4px 0 0; padding-left: 20px; }
"""

TABS: tuple[tuple[str, str, str], ...] = (
    ("basic", "1. 基本信息", "/apply/tab1"),
    ("materials", "2. 材料上传", "/apply/tab2"),
    ("confirm", "3. 确认提交", "/apply/tab3"),
)


def layout(title: str, body: str, *, page: str, script: str = "") -> str:
    script_html = f"\n<script>{script}</script>" if script else ""
    return (
        "<!doctype html>\n"
        '<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{escape(title)}</title>\n<style>{STYLE}</style>\n</head>\n<body>\n"
        '<header class="mock-banner" data-testid="mock-banner">'
        "本地模拟站 · 任何行为都不代表真实政务系统</header>\n"
        f'<main data-testid="{page}">\n{body}\n</main>{script_html}\n</body>\n</html>\n'
    )


def _error_box(message: str, *, code: str, testid: str = "form-error") -> str:
    return (
        f'<p class="error" data-testid="{testid}" data-code="{escape(code, quote=True)}">'
        f"{escape(message)}</p>"
    )


def _field_error(errors: dict[str, str], field: str) -> str:
    message = errors.get(field)
    if not message:
        return ""
    return f'<p class="field-error" data-testid="error-{field}">{escape(message)}</p>'


def _tabs_html(current: str, done: set[str]) -> str:
    links = []
    for key, label, href in TABS:
        state = "current" if key == current else ("done" if key in done else "todo")
        links.append(
            f'<a href="{href}" data-testid="tab-{key}" data-state="{state}">{escape(label)}</a>'
        )
    return f'<nav class="tabs">{"".join(links)}</nav>'


def _apply_header(session: ApplySession, service: Service) -> str:
    return (
        '<p class="hint" data-testid="apply-context">办理事项：'
        f'<strong data-testid="apply-service">{escape(service.name)}</strong> · 办理人 '
        f'<span data-testid="apply-user">{escape(session.username)}</span></p>'
    )


# --------------------------------------------------------------------- 账号站


def login_page(
    *,
    captcha_id: str,
    captcha_svg: str,
    username: str = "",
    error: str = "",
    error_code: str = "",
    locked: bool = False,
    max_failures: int = 3,
) -> str:
    locked_box = (
        '<p class="error" data-testid="login-locked" data-code="account_locked">'
        "账号已锁定：连续失败次数达到上限。模拟站不自动解锁，请调用 "
        "<code>POST /__mock__/reset</code> 重置。</p>"
        if locked
        else ""
    )
    error_box = _error_box(error, code=error_code, testid="login-error") if error else ""
    body = f"""
<section class="card">
  <h1>账号登录</h1>
  <p class="hint" data-testid="login-hint">演示账号 <code>demo / demo1234</code>；
  连续失败 {max_failures} 次后账号锁定。</p>
  {locked_box}
  {error_box}
  <form method="post" action="/login" data-testid="login-form">
    <label>账号
      <input type="text" name="username" value="{escape(username, quote=True)}"
             data-testid="input-username" autocomplete="username" required>
    </label>
    <label>密码
      <input type="password" name="password" data-testid="input-password"
             autocomplete="current-password" required>
    </label>
    <label>图形验证码
      <span class="captcha-row">
        <img src="{captcha_svg}" width="120" height="44" alt="图形验证码"
             data-testid="captcha-image">
        <input type="text" name="captcha_code" data-testid="input-captcha"
               autocomplete="off" required>
      </span>
    </label>
    <input type="hidden" name="captcha_id" value="{escape(captcha_id, quote=True)}"
           data-testid="captcha-id">
    <button type="submit" data-testid="login-submit">登录</button>
  </form>
  <p><a href="/login/qr" data-testid="link-qr-login">用手机扫码登录</a></p>
</section>
"""
    return layout("登录 · 模拟站", body, page="login-page")


def qr_login_page(
    *,
    ticket: str,
    qr_svg: str,
    ttl_seconds: float,
    status: str = "pending",
) -> str:
    script = """
const node = document.querySelector('[data-testid="qr-status"]');
const ticket = document.querySelector('[data-testid="qr-ticket"]').dataset.ticket;
async function poll() {
  const response = await fetch('/login/qr/' + encodeURIComponent(ticket) + '/status');
  const data = await response.json();
  node.textContent = data.status;
  node.dataset.state = data.status;
  if (data.status === 'confirmed' && data.session_url) {
    window.location.href = data.session_url;
    return;
  }
  if (data.status === 'expired' || data.status === 'consumed') { return; }
  window.setTimeout(poll, 1000);
}
window.setTimeout(poll, 400);
"""
    body = f"""
<section class="card">
  <h1>扫码登录</h1>
  <p class="hint">这是本地模拟站：二维码是占位图案，不承载可扫描的真实编码。
  「手机端确认」由 <code>POST /__mock__/qr/&lt;ticket&gt;/confirm</code> 模拟。</p>
  <p><img src="{qr_svg}" width="250" height="250" alt="扫码登录占位图案"
          data-testid="qr-image"></p>
  <p>票据 <code data-testid="qr-ticket"
      data-ticket="{escape(ticket, quote=True)}">{escape(ticket)}</code></p>
  <p>状态 <strong data-testid="qr-status"
      data-state="{escape(status, quote=True)}">{escape(status)}</strong>
     <span class="hint" data-testid="qr-ttl">（{ttl_seconds:g} 秒内有效）</span></p>
  <p><a href="/login" data-testid="link-password-login">改用账号密码登录</a></p>
</section>
"""
    return layout("扫码登录 · 模拟站", body, page="qr-login-page", script=script)


def services_page(*, username: str, services: Sequence[Service]) -> str:
    items = []
    for service in services:
        badge = '<span class="tag" data-testid="service-pilot">试点</span>' if service.pilot else ""
        items.append(
            f'<li class="service" data-testid="service-item" '
            f'data-service-id="{escape(service.id, quote=True)}">\n'
            f"  <h2>{escape(service.name)} {badge}</h2>\n"
            f'  <p class="hint">{escape(service.summary)}</p>\n'
            f'  <a class="button" href="/handoff/{escape(service.id, quote=True)}" '
            f'data-testid="service-start">去办理</a>\n</li>'
        )
    body = f"""
<section class="card">
  <h1>可办理事项</h1>
  <p class="hint">当前登录：<span data-testid="current-user">{escape(username)}</span></p>
</section>
<section class="card">
  <ul class="services">
    {"".join(items)}
  </ul>
</section>
"""
    return layout("事项列表 · 模拟站", body, page="services-page")


# --------------------------------------------------------------------- 办理站


def apply_basic_page(
    *, session: ApplySession, service: Service, errors: dict[str, str]
) -> str:
    draft = session.draft
    options = "".join(
        f'<option value="{escape(code, quote=True)}"'
        f'{" selected" if code == draft.domicile_type else ""}>{escape(label)}</option>'
        for code, label in DOMICILE_TYPES
    )
    checks = "".join(
        f'<label class="inline"><input type="checkbox" name="situations" '
        f'value="{escape(code, quote=True)}"'
        f'{" checked" if code in draft.situations else ""} '
        f'data-testid="field-situation-{escape(code, quote=True)}"> {escape(label)}</label>'
        for code, label in SITUATIONS
    )
    body = f"""
{_apply_header(session, service)}
{_tabs_html("basic", set())}
<form method="post" action="/apply/tab1" class="card" data-testid="basic-form">
  <h1>基本信息</h1>
  <label>户籍类型
    <select name="domicile_type" data-testid="field-domicile">{options}</select>
  </label>
  {_field_error(errors, "domicile_type")}
  <fieldset data-testid="field-situations">
    <legend>办理情形（可多选）</legend>
    {checks}
  </fieldset>
  {_field_error(errors, "situations")}
  <label>联系电话
    <input type="text" name="phone" value="{escape(draft.phone, quote=True)}"
           data-testid="field-phone" inputmode="numeric">
  </label>
  {_field_error(errors, "phone")}
  <label>联系地址
    <input type="text" name="address" value="{escape(draft.address, quote=True)}"
           data-testid="field-address">
  </label>
  {_field_error(errors, "address")}
  <p><button type="submit" data-testid="basic-submit">保存并下一步</button></p>
</form>
"""
    return layout("基本信息 · 模拟站", body, page="apply-basic-page")


def apply_materials_page(
    *,
    session: ApplySession,
    service: Service,
    config: MockConfig,
    count: int,
    errors: list[str],
) -> str:
    rows = []
    for item in session.draft.attachments:
        rows.append(_attachment_row(item))
    limit_line = (
        f"类型：{escape('、'.join(config.allowed_upload_types))}；"
        f"单个不超过 {config.max_upload_bytes // 1024} KB；"
        f"共需 {config.min_attachments}-{config.max_attachments} 个"
    )
    if rows:
        list_html = f'<ul class="services" data-testid="attachment-list">{"".join(rows)}</ul>'
    else:
        list_html = '<p class="hint" data-testid="attachment-empty">还没有上传任何材料。</p>'
    error_html = "".join(
        _error_box(message, code="upload_rejected", testid="upload-error") for message in errors
    )
    body = f"""
{_apply_header(session, service)}
{_tabs_html("materials", {"basic"} if session.draft.domicile_type else set())}
<section class="card">
  <h1>材料上传</h1>
  <p class="hint" data-testid="attachment-limits">{limit_line}</p>
  <p data-testid="attachment-count" data-count="{count}">
    已上传 {count} / {config.max_attachments}</p>
  {error_html}
  <form method="post" action="/apply/tab2/upload" enctype="multipart/form-data"
        data-testid="upload-form">
    <label>选择材料（可多选）
      <input type="file" name="files" multiple data-testid="upload-input"
             accept="{escape(','.join(config.allowed_upload_types), quote=True)}">
    </label>
    <p><button type="submit" data-testid="upload-submit">上传</button></p>
  </form>
  {list_html}
</section>
"""
    return layout("材料上传 · 模拟站", body, page="apply-materials-page")


def _attachment_row(item: Attachment) -> str:
    file_id = escape(item.file_id, quote=True)
    return (
        f'<li class="service" data-testid="attachment-item" data-file-id="{file_id}">\n'
        f"  <h2>{escape(item.filename)}</h2>\n"
        f'  <p class="hint">{escape(item.content_type)} · {item.size} 字节</p>\n'
        f'  <form method="post" action="/apply/tab2/delete" data-testid="attachment-delete-form">'
        f'<input type="hidden" name="file_id" value="{file_id}">'
        f'<button class="secondary" type="submit" data-testid="attachment-delete">删除</button>'
        "</form>\n</li>"
    )


def apply_confirm_page(
    *, session: ApplySession, service: Service, blockers: Sequence[str]
) -> str:
    draft = session.draft
    domicile = dict(DOMICILE_TYPES).get(draft.domicile_type or "", "未填写")
    situations = "、".join(dict(SITUATIONS).get(code, code) for code in draft.situations)
    situations = situations or "未填写"
    blocker_html = ""
    if blockers:
        items = "".join(f"<li>{escape(text)}</li>" for text in blockers)
        blocker_html = (
            '<div class="card" data-testid="submit-blockers">\n'
            "<h2>还不能提交</h2>\n"
            f'<ul class="plain" data-testid="blocker-list">{items}</ul>\n</div>'
        )
    disabled = " disabled" if blockers else ""
    body = f"""
{_apply_header(session, service)}
{_tabs_html("confirm", {"basic", "materials"})}
<section class="card" data-testid="summary">
  <h1>确认提交</h1>
  <table class="summary">
    <tr><th>办理事项</th><td data-testid="summary-service">{escape(service.name)}</td></tr>
    <tr><th>户籍类型</th><td data-testid="summary-domicile">{escape(domicile)}</td></tr>
    <tr><th>办理情形</th><td data-testid="summary-situations">{escape(situations)}</td></tr>
    <tr><th>联系电话</th><td data-testid="summary-phone">{escape(draft.phone or "未填写")}</td>
    </tr>
    <tr><th>联系地址</th><td data-testid="summary-address">
      {escape(draft.address or "未填写")}</td></tr>
    <tr><th>材料</th><td data-testid="summary-attachments">{len(draft.attachments)} 个</td></tr>
  </table>
</section>
{blocker_html}
<form method="post" action="/apply/submit" class="card" data-testid="submit-form">
  <button type="submit" data-testid="submit-application"{disabled}>提交申请</button>
</form>
"""
    return layout("确认提交 · 模拟站", body, page="apply-confirm-page")


def receipt_page(*, receipt: Receipt, service_name: str) -> str:
    body = f"""
<section class="card" data-testid="receipt-section">
  <h1>受理回执</h1>
  <p>回执号 <code class="receipt" data-testid="receipt-no">{escape(receipt.no)}</code></p>
  <table class="summary">
    <tr><th>状态</th><td data-testid="receipt-status" data-state="{escape(receipt.status)}">
      {escape(receipt.status)}</td></tr>
    <tr><th>办理事项</th><td data-testid="receipt-service">{escape(service_name)}</td></tr>
    <tr><th>办理人</th><td data-testid="receipt-user">{escape(receipt.username)}</td></tr>
    <tr><th>材料数量</th><td data-testid="receipt-attachments">{receipt.attachments}</td></tr>
  </table>
  <p class="hint">查询：<code>GET /receipt/{escape(receipt.no, quote=True)}/status</code>
  返回 JSON，供执行器回读核验；不要凭「提交按钮点过了」判断成功。</p>
</section>
"""
    return layout("受理回执 · 模拟站", body, page="receipt-page")


def error_page(
    *,
    status: int,
    code: str,
    message: str,
    detail: str = "",
    back_href: str = "/login",
    back_label: str = "返回登录",
) -> str:
    detail_html = (
        f'<p class="hint" data-testid="error-detail">{escape(detail)}</p>' if detail else ""
    )
    body = f"""
<section class="card" data-testid="error-page" data-status="{status}">
  <h1>办理没有完成</h1>
  <p class="error" data-testid="error-code" data-code="{escape(code, quote=True)}">
    错误码 {escape(code)}（HTTP {status}）</p>
  <p data-testid="error-message">{escape(message)}</p>
  {detail_html}
  <p><a href="{escape(back_href, quote=True)}" data-testid="error-back">
    {escape(back_label)}</a></p>
</section>
"""
    return layout("出错 · 模拟站", body, page="error-page")
