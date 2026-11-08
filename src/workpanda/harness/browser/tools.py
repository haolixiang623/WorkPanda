"""七个浏览器工具：走到「进入填写页」为止，写操作全部由策略层挡在门外。

分工：

- 工具只管**怎么操作**（定位、点击、读文本），**能不能操作**一律问
  `BrowserPolicy`；
- 失败一律抛 `ToolError`（带明确失败码），绝不返回"看起来成功了"的文本——
  误报成功比失败严重得多；
- 不设内部超时，时限只有 `ToolSpec.timeout_seconds` 一个来源（超时=结果未知，
  由 harness 记 `TIMEOUT`，不会自动重试非幂等动作）。

定位约定：`selector` 是 CSS 选择器，`index` 从 0 开始。**匹配到多个而调用方没给
`index` 时直接失败**（`ambiguous_target`）而不是"默默点第一个"——列表页上一个
`data-testid` 常被复用成 N 份，点错行是这类工具最典型的错误。
"""

from __future__ import annotations

from typing import Any

from workpanda.harness.browser.policy import BrowserPolicy
from workpanda.harness.browser.session import (
    BrowserSession,
    BrowserSessions,
    is_timeout_error,
)
from workpanda.harness.models import RiskLevel, ToolSpec
from workpanda.harness.tools import ToolContext, ToolError

# 取元素自身的属性，用来判断"点了会不会写数据"。判断依据只能是元素本身，
# 不能是模型说的那句话。
_DESCRIBE_JS = """
(el) => {
  const attr = (name) => (el.getAttribute ? el.getAttribute(name) : null);
  const tag = (el.tagName || '').toLowerCase();
  const raw = attr('aria-label') || attr('title') || '';
  const text = raw || el.innerText || el.value || '';
  const href = tag === 'a' ? String(attr('href') || '') : '';
  return {
    tag,
    type: attr('type'),
    testid: attr('data-testid'),
    hasForm: !!el.form,
    downloads: tag === 'a' && (
      el.hasAttribute('download') || /\\.(zip|pdf|docx?|xlsx?|csv)$/i.test(href)
    ),
    name: String(text).replace(/\\s+/g, ' ').trim().slice(0, 120),
  };
}
"""

_TAG_JS = "(el) => (el.tagName || '').toLowerCase()"

# 读一个元素"给人看的内容"。表单控件读的是当前值——填完之后能回读核对，
# 否则"填了没有"只能靠猜。密码字段不回读：它进 trace 就等于进了日志。
_READ_JS = """
(el) => {
  const tag = (el.tagName || '').toLowerCase();
  if (tag === 'input' || tag === 'textarea') {
    const type = (el.getAttribute('type') || 'text').toLowerCase();
    if (type === 'password') { return '(密码字段，不回读)'; }
    if (type === 'checkbox' || type === 'radio') {
      return el.checked ? '已选中' : '未选中';
    }
    return String(el.value || '');
  }
  const text = el.innerText || '';
  return text.trim() ? text : String(el.textContent || '');
}
"""

_OPTIONS_JS = """
(el) => Array.from(el.options || []).map(
  (option) => ({value: option.value, label: (option.text || '').trim()})
)
"""

_INDEX_SCHEMA: dict[str, Any] = {
    "type": "integer",
    "minimum": 0,
    "maximum": 50,
    "description": "选择器命中多个时的第几个（从 0 开始）；不给且命中多个则直接失败。",
}


class _BrowserTool:
    """公共骨架：会话取用、规格声明、线程内执行。"""

    tool_name: str = ""
    tool_description: str = ""
    parameters: dict[str, Any] = {}
    timeout_seconds: float = 30.0
    idempotent: bool = True
    max_calls: int | None = None
    sensitive_fields: list[str] = []

    def __init__(self, sessions: BrowserSessions, policy: BrowserPolicy) -> None:
        self._sessions = sessions
        self._policy = policy
        self.spec = ToolSpec(
            name=self.tool_name,
            description=self.tool_description,
            parameters=self.parameters,
            risk=RiskLevel.MEDIUM,
            idempotent=self.idempotent,
            timeout_seconds=self.timeout_seconds,
            max_calls=self.max_calls,
            sensitive_fields=list(self.sensitive_fields),
        )

    def invoke(self, arguments: dict[str, Any], context: ToolContext) -> str:
        session = self._sessions.for_task(context.task_id)
        return session.run(
            lambda page: self.act(page, arguments, session), label=self.tool_name
        )

    def act(self, page: Any, arguments: dict[str, Any], session: BrowserSession) -> str:
        raise NotImplementedError


# ---------- 定位与读文本 ----------


def _locator(page: Any, selector: str, index: int | None) -> tuple[Any, int]:
    """按 `selector` 取元素。0 个→失败；多个且没给 index→失败（不猜）。"""

    text = (selector or "").strip()
    if not text:
        raise ToolError("invalid_selector", "定位点不能为空。")
    locator = page.locator(text)
    count = locator.count()
    if count == 0:
        raise ToolError(
            "element_not_found",
            f"页面上找不到 `{text}`：页面可能已改版，请转人工，不要换选择器硬凑。",
        )
    if index is None:
        if count > 1:
            raise ToolError(
                "ambiguous_target",
                f"`{text}` 匹配到 {count} 个元素，无法确定操作哪一个；"
                "先用 browser_read 看清结构，再用 index 指定（从 0 开始）。",
            )
        return locator, count
    if not 0 <= index < count:
        raise ToolError(
            "element_not_found",
            f"`{text}` 只匹配到 {count} 个元素，没有第 {index} 个。",
        )
    return locator.nth(index), count


def _text_of(locator: Any) -> str:
    """取元素的可见文本；表单控件取当前值（密码不回读）。"""

    return str(locator.evaluate(_READ_JS) or "")


def _clip(policy: BrowserPolicy, text: str, limit: int | None = None) -> str:
    clipped = policy.clip(text)
    if limit and len(clipped) > limit:
        return clipped[:limit] + "…"
    return clipped


def _describe(handle: Any) -> dict[str, Any]:
    data = handle.evaluate(_DESCRIBE_JS) or {}
    return {
        "tag": str(data.get("tag") or ""),
        "type": data.get("type"),
        "testid": data.get("testid"),
        "hasForm": bool(data.get("hasForm")),
        "downloads": bool(data.get("downloads")),
        "name": str(data.get("name") or ""),
    }


def _submit_like(desc: dict[str, Any]) -> bool:
    """这个元素点下去会不会提交表单。

    `<button>` 不写 `type` 时，只要它有 form 宿主就**默认是 submit**——这条
    默认值正是"看起来是个普通按钮、其实是提交"的来源。
    """

    tag = desc["tag"]
    kind = (desc["type"] or "").lower()
    if tag == "input" and kind in {"submit", "image"}:
        return True
    if tag == "button":
        return kind == "submit" or (kind == "" and desc["hasForm"])
    return False


def _page_report(page: Any, policy: BrowserPolicy, *, header: str) -> str:
    return (
        f"{header}\n标题: {page.title()}\n地址: {page.url}\n"
        f"正文:\n{policy.clip(page.inner_text('body'))}"
    )


# ---------- 七个工具 ----------


class BrowserOpenTool(_BrowserTool):
    tool_name = "browser_open"
    tool_description = "在白名单域名内打开页面（http/https），返回地址、标题与可见正文。只读。"
    parameters = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "要打开的 http/https 地址",
                "maxLength": 500,
            }
        },
        "required": ["url"],
        "additionalProperties": False,
    }
    timeout_seconds = 45.0

    def act(self, page: Any, arguments: dict[str, Any], session: BrowserSession) -> str:
        # 先校验再导航：白名单外的地址一次都不发出去。
        url = self._policy.check_url(str(arguments["url"]))
        page.goto(url, wait_until="domcontentloaded")
        return _page_report(page, self._policy, header=f"已打开 {url}")


class BrowserReadTool(_BrowserTool):
    tool_name = "browser_read"
    tool_description = (
        "读页面可见文本：不传 selector 读整页；传 selector 命中多个时列出编号，"
        "供后续用 index 精确定位。只读。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "selector": {
                "type": "string",
                "description": "CSS 选择器；不传则读整页正文",
                "maxLength": 300,
            },
            "index": _INDEX_SCHEMA,
            "limit": {
                "type": "integer",
                "minimum": 50,
                "maximum": 4000,
                "description": "本次最多返回多少字",
            },
        },
        "additionalProperties": False,
    }

    def act(self, page: Any, arguments: dict[str, Any], session: BrowserSession) -> str:
        selector = arguments.get("selector")
        index = arguments.get("index")
        limit = arguments.get("limit")
        if not selector:
            body = page.inner_text("body")
            return f"地址: {page.url}\n正文:\n{_clip(self._policy, body, limit)}"

        text = str(selector).strip()
        if not text:
            raise ToolError("invalid_selector", "定位点不能为空。")
        locator = page.locator(text)
        count = locator.count()
        if count == 0:
            raise ToolError(
                "element_not_found", f"页面上找不到 `{text}`：页面可能已改版，请转人工。"
            )
        if index is None:
            if count == 1:
                content = _text_of(locator)
                return f"地址: {page.url}\n`{text}`:\n{_clip(self._policy, content, limit)}"
            lines = [f"地址: {page.url}", f"`{text}` 匹配到 {count} 个元素："]
            for position in range(min(count, 10)):
                snippet = _text_of(locator.nth(position))[:80].replace("\n", " ")
                lines.append(f"[{position}] {snippet or '(无文本)'}")
            if count > 10:
                lines.append(f"…（其余 {count - 10} 个未列出）")
            return "\n".join(lines)

        target, _ = _locator(page, text, index)
        content = _text_of(target)
        return f"地址: {page.url}\n`{text}`[{index}]:\n{_clip(self._policy, content, limit)}"


class BrowserClickTool(_BrowserTool):
    tool_name = "browser_click"
    tool_description = (
        "点击页面元素（链接、普通按钮）。提交类控件、上传、删除、下载一律拒绝；"
        "被拒时不会有任何点击被发出。"
    )
    # 非幂等：点两次可能是两次副作用，所以一任务里同一个元素只允许成功点一次。
    idempotent = False
    # 但一次执行里往往要走多步（登录 → 进事项 → ...），不能只给 1 次。
    max_calls = 8
    parameters = {
        "type": "object",
        "properties": {
            "selector": {"type": "string", "description": "CSS 选择器", "maxLength": 300},
            "index": _INDEX_SCHEMA,
        },
        "required": ["selector"],
        "additionalProperties": False,
    }

    def act(self, page: Any, arguments: dict[str, Any], session: BrowserSession) -> str:
        selector = str(arguments["selector"])
        index = arguments.get("index")
        locator, count = _locator(page, selector, index)

        handle = locator.element_handle()
        if handle is None:
            raise ToolError(
                "element_not_found",
                f"`{selector}` 在点击前已从页面上消失（页面可能刚刚重绘）。",
            )
        desc = _describe(handle)
        # 闸门在派发点击之前：被拒时点击一次都没发出去。
        self._policy.check_click(
            testid=desc["testid"],
            tag=desc["tag"],
            input_type=desc["type"],
            accessible_name=desc["name"],
            submit_like=_submit_like(desc),
            downloads=desc["downloads"],
        )
        # 点的是刚检查过的那个句柄，不重新查一遍选择器——重查会让"检查的"与
        # "点的"变成两个元素。
        try:
            handle.click()
        except Exception as exc:  # noqa: BLE001 - 下面按类型分类，其余原样抛
            message = str(exc)
            if is_timeout_error(exc):
                raise ToolError(
                    "element_timeout",
                    f"`{selector}` 在限期内不可点击（被遮挡或未渲染），请转人工。",
                ) from exc
            if "not attached" in message or "detached" in message:
                raise ToolError(
                    "element_not_found", f"点击瞬间 `{selector}` 已脱离页面。"
                ) from exc
            raise
        where = "" if count == 1 else f"（匹配 {count} 个，点了第 {index or 0} 个）"
        return _page_report(page, self._policy, header=f"已点击 `{selector}`{where}")


class BrowserFillTool(_BrowserTool):
    tool_name = "browser_fill"
    tool_description = "向文本输入框写入内容（先清空再填）。下拉请用 browser_select。"
    # 填值是把字段设成绝对值，重复填同一个值不会叠加副作用。
    idempotent = True
    # 填进去的多半是群众个人信息：审计与 trace 里不该出现明文。
    sensitive_fields = ["value"]
    parameters = {
        "type": "object",
        "properties": {
            "selector": {"type": "string", "description": "CSS 选择器", "maxLength": 300},
            "value": {"type": "string", "description": "要写入的文本", "maxLength": 500},
            "index": _INDEX_SCHEMA,
        },
        "required": ["selector", "value"],
        "additionalProperties": False,
    }

    def act(self, page: Any, arguments: dict[str, Any], session: BrowserSession) -> str:
        selector = str(arguments["selector"])
        value = str(arguments["value"])
        locator, _ = _locator(page, selector, arguments.get("index"))
        # 先记账再填：落盘时要把这个值从 DOM 里抹掉。
        session.remember_value(value)
        locator.fill(value)
        return f"已填写 `{selector}`（{len(value)} 字）。"


class BrowserSelectTool(_BrowserTool):
    tool_name = "browser_select"
    tool_description = "选择下拉框的选项（按 option 的 value 或显示文本匹配）。"
    idempotent = True
    parameters = {
        "type": "object",
        "properties": {
            "selector": {"type": "string", "description": "CSS 选择器", "maxLength": 300},
            "value": {"type": "string", "description": "选项的 value 或显示文本", "maxLength": 200},
            "index": _INDEX_SCHEMA,
        },
        "required": ["selector", "value"],
        "additionalProperties": False,
    }

    def act(self, page: Any, arguments: dict[str, Any], session: BrowserSession) -> str:
        selector = str(arguments["selector"])
        value = str(arguments["value"])
        locator, _ = _locator(page, selector, arguments.get("index"))

        tag = str(locator.evaluate(_TAG_JS) or "")
        if tag != "select":
            raise ToolError(
                "not_a_select",
                f"`{selector}` 是 <{tag or '未知'}>，不是 <select>；单选/多选用 browser_check。",
            )
        options = locator.evaluate(_OPTIONS_JS) or []
        values = [str(item.get("value", "")) for item in options]
        labels = [str(item.get("label", "")) for item in options]
        if value in values:
            locator.select_option(value=value)
        elif value in labels:
            locator.select_option(label=value)
        else:
            available = "、".join(item for item in labels[:12] if item) or "(空)"
            raise ToolError(
                "option_not_found",
                f"`{selector}` 没有 {value!r} 这个选项；可选：{available}",
            )
        return f"已选择 `{selector}` = {value}"


class BrowserCheckTool(_BrowserTool):
    tool_name = "browser_check"
    tool_description = "勾选或取消勾选复选框/单选框。单选框只能改选，不能取消。"
    idempotent = True
    parameters = {
        "type": "object",
        "properties": {
            "selector": {"type": "string", "description": "CSS 选择器", "maxLength": 300},
            "checked": {
                "type": "boolean",
                "description": "true 勾选（默认），false 取消勾选",
            },
            "index": _INDEX_SCHEMA,
        },
        "required": ["selector"],
        "additionalProperties": False,
    }

    def act(self, page: Any, arguments: dict[str, Any], session: BrowserSession) -> str:
        selector = str(arguments["selector"])
        checked = bool(arguments.get("checked", True))
        locator, _ = _locator(page, selector, arguments.get("index"))

        handle = locator.element_handle()
        desc = _describe(handle) if handle is not None else {"tag": "", "type": None}
        kind = (desc["type"] or "").lower()
        if desc["tag"] != "input" or kind not in {"checkbox", "radio"}:
            raise ToolError(
                "not_a_checkbox",
                f"`{selector}` 是 <{desc['tag'] or '未知'}/{kind or '无 type'}>，"
                "不是复选框或单选框。",
            )
        if locator.is_checked() == checked:
            state = "选中" if checked else "未选中"
            return f"`{selector}` 已是{state}状态，未做改动。"
        if kind == "radio" and not checked:
            raise ToolError(
                "not_uncheckable", "单选框不能取消选中，只能改选另一个选项。"
            )
        if checked:
            locator.check()
        else:
            locator.uncheck()
        return f"已{'勾选' if checked else '取消勾选'} `{selector}`"


class BrowserWaitTool(_BrowserTool):
    tool_name = "browser_wait"
    tool_description = "等待元素出现/隐藏。等不到就明确失败，不会自动重试。"
    idempotent = True
    parameters = {
        "type": "object",
        "properties": {
            "selector": {"type": "string", "description": "CSS 选择器", "maxLength": 300},
            "state": {
                "type": "string",
                "enum": ["visible", "attached", "hidden"],
                "description": "等到哪种状态，默认 visible",
            },
        },
        "required": ["selector"],
        "additionalProperties": False,
    }
    timeout_seconds = 30.0

    def act(self, page: Any, arguments: dict[str, Any], session: BrowserSession) -> str:
        selector = str(arguments["selector"]).strip()
        if not selector:
            raise ToolError("invalid_selector", "定位点不能为空。")
        state = str(arguments.get("state") or "visible")
        try:
            page.wait_for_selector(
                selector, state=state, timeout=self._policy.element_timeout_ms
            )
        except Exception as exc:  # noqa: BLE001 - 只翻译超时，其余原样抛
            if is_timeout_error(exc):
                raise ToolError(
                    "element_timeout",
                    f"等待 `{selector}` 变成 {state} 超时：页面可能已改版或没加载出来，"
                    "请转人工。",
                ) from exc
            raise
        return f"`{selector}` 已变成 {state}（地址: {page.url}）"


def browser_tools(sessions: BrowserSessions, policy: BrowserPolicy) -> list[Any]:
    """构造七个工具。调用方负责注册与关闭会话。"""

    return [
        tool(sessions, policy)
        for tool in (
            BrowserOpenTool,
            BrowserReadTool,
            BrowserClickTool,
            BrowserFillTool,
            BrowserSelectTool,
            BrowserCheckTool,
            BrowserWaitTool,
        )
    ]
