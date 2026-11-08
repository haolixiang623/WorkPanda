"""浏览器工具的策略回归：**不需要浏览器**。

真正会出事的三类判断（域名、副作用、失败语义）都在 `policy` 里，而这一层是纯
函数。所以它们必须能在任何环境下跑——否则最该被守住的红线会以"本机没装浏览器"
为由长期无人验证，直到某次真的放行了一个提交按钮。

第七节顺带回归"超时=结果未知、不重试"这条契约：它由 harness 的 `ToolGrant` 保证，
与浏览器无关，但正是 #79 的验收项之一。
"""

from __future__ import annotations

import time
from typing import Any
from uuid import uuid4

import pytest

from workpanda.config import Settings
from workpanda.harness.browser import BrowserSessions, browser_tools, policy_from_hosts
from workpanda.harness.browser.policy import BrowserPolicy
from workpanda.harness.builtin import default_registry
from workpanda.harness.models import ToolCall, ToolOutcome, ToolSpec
from workpanda.harness.tools import (
    ToolContext,
    ToolError,
    ToolGrant,
    ToolRegistry,
)


def policy(**kwargs: Any) -> BrowserPolicy:
    hosts = kwargs.pop("allowed_hosts", ("127.0.0.1:8101", "127.0.0.1:8102"))
    return policy_from_hosts(tuple(hosts), **kwargs)


# ---------- 一、域名 ----------


def test_scheme_and_credentials_are_refused() -> None:
    browser = policy()

    with pytest.raises(ToolError) as file_error:
        browser.check_url("file:///etc/passwd")
    assert file_error.value.code == "scheme_not_allowed"

    with pytest.raises(ToolError) as js_error:
        browser.check_url("javascript:alert(1)")
    assert js_error.value.code == "scheme_not_allowed"

    with pytest.raises(ToolError) as credentials:
        browser.check_url("http://user:pass@127.0.0.1:8101/login")
    assert credentials.value.code == "credentials_in_url"


def test_host_must_be_listed_exactly_or_by_suffix() -> None:
    browser = policy(allowed_hosts=("example.gov.cn", ".gov.cn"))

    assert browser.check_url("https://example.gov.cn/apply")
    assert browser.check_url("https://sub.example.gov.cn/apply")
    # 后缀匹配必须是"点分后缀"，不能让 evil-gov.cn 蹭过去。
    with pytest.raises(ToolError) as suffix:
        browser.check_url("https://evil-gov.cn/apply")
    assert suffix.value.code == "host_not_allowed"

    with pytest.raises(ToolError) as other:
        browser.check_url("https://example.com/apply")
    assert other.value.code == "host_not_allowed"


def test_whitelist_entry_with_port_only_allows_that_origin() -> None:
    """同一主机两个端口是两个 origin：只列了一个就不能去另一个。"""

    browser = policy(allowed_hosts=("127.0.0.1:8101",))

    assert browser.check_url("http://127.0.0.1:8101/login")
    with pytest.raises(ToolError) as landed:
        browser.check_landing("http://127.0.0.1:8102/apply?ticket=t")
    assert landed.value.code == "redirect_host_not_allowed"
    assert "127.0.0.1:8102" in landed.value.message


def test_whitelist_entry_without_port_allows_any_port() -> None:
    browser = policy(allowed_hosts=("127.0.0.1",))

    assert browser.check_url("http://127.0.0.1:8101/login")
    assert browser.check_landing("http://127.0.0.1:8102/apply")


def test_default_port_is_the_same_origin_as_explicit_port() -> None:
    browser = policy(allowed_hosts=("a.example:443",))

    # 不写端口的 https 就是 443，不能因为"没写"而被拒，也不能因为"没写"而放行 8443。
    assert browser.check_url("https://a.example/x")
    with pytest.raises(ToolError):
        browser.check_url("https://a.example:8443/x")


def test_ipv6_entry_needs_brackets_and_port() -> None:
    browser = policy(allowed_hosts=("[::1]:8101",))

    assert browser.check_url("http://[::1]:8101/login")
    with pytest.raises(ToolError):
        browser.check_url("http://[::1]:8102/login")


def test_landing_on_unlisted_second_site_is_refused() -> None:
    """跨站交接的落地站点必须显式列出——只校验入口等于没校验。"""

    browser = policy(allowed_hosts=("127.0.0.1:8101",))

    with pytest.raises(ToolError) as landed:
        browser.check_landing("http://127.0.0.1:8102/apply?ticket=t")
    assert landed.value.code == "redirect_host_not_allowed"


# ---------- 二、副作用闸门 ----------


def _click(browser: BrowserPolicy, **kwargs: Any) -> None:
    payload: dict[str, Any] = {
        "testid": None,
        "tag": "button",
        "input_type": "button",
        "accessible_name": "下一步",
        "submit_like": False,
    }
    payload.update(kwargs)
    browser.check_click(**payload)


def test_read_only_click_passes() -> None:
    _click(policy(), testid="service-start", tag="a", accessible_name="去办理")


def test_click_on_forbidden_testid_is_refused() -> None:
    for testid in ("submit-application", "upload-submit", "attachment-delete"):
        with pytest.raises(ToolError) as refused:
            _click(policy(), testid=testid, accessible_name="下一步")
        assert refused.value.code == "submit_click_blocked"
        assert testid in refused.value.message


def test_click_on_commit_words_is_refused() -> None:
    for name in ("提交申请", "上传材料", "确认并提交", "删除", "去支付", "签署协议"):
        with pytest.raises(ToolError) as refused:
            _click(policy(), accessible_name=name)
        assert refused.value.code == "submit_click_blocked", name


def test_download_link_is_refused() -> None:
    with pytest.raises(ToolError) as refused:
        _click(policy(), tag="a", accessible_name="下载受理回执")
    assert refused.value.code == "download_blocked"

    # 只有一张图的链接没有可读文本，只能靠元素自己的 download / 文件后缀判断。
    with pytest.raises(ToolError) as attribute:
        _click(policy(), tag="a", accessible_name="", downloads=True)
    assert attribute.value.code == "download_blocked"


def test_submit_control_is_refused_unless_explicitly_permitted() -> None:
    """提交类控件默认全拒：登录按钮也必须逐个登记过才点得动。"""

    with pytest.raises(ToolError) as refused:
        _click(policy(), testid="login-submit", accessible_name="登录", submit_like=True)
    assert refused.value.code == "submit_click_blocked"
    assert "HARNESS_BROWSER_PERMITTED_CLICK_TESTIDS" in refused.value.message

    # 没有 testid 的提交控件连"登记"这条路都没有，只能转人工。
    with pytest.raises(ToolError) as anonymous:
        _click(policy(), accessible_name="登录", submit_like=True)
    assert "转人工" in anonymous.value.message

    _click(
        policy(permitted_submit_testids=frozenset({"login-submit"})),
        testid="login-submit",
        accessible_name="登录",
        submit_like=True,
    )


def test_permission_does_not_override_the_name_denylist() -> None:
    """放行名单只解决"是提交控件"，不解决"这个词就该拒"。"""

    browser = policy(permitted_submit_testids=frozenset({"login-submit"}))

    with pytest.raises(ToolError) as refused:
        _click(browser, testid="login-submit", accessible_name="提交登录", submit_like=True)
    assert refused.value.code == "submit_click_blocked"


def test_clip_keeps_short_text_and_marks_truncation() -> None:
    browser = policy(max_read_chars=10)

    assert browser.clip("短文本") == "短文本"
    clipped = browser.clip("一二三四五六七八九十十一")
    assert clipped.startswith("一二三四五六七八九十")
    assert "已截断" in clipped and "12" in clipped


# ---------- 三、注册与授权 ----------


def test_browser_tools_are_absent_until_a_host_list_is_configured() -> None:
    # 开关关着、或开关开着但没配白名单，都不注册——默认不可用比默认全开安全。
    registry, _ = default_registry()
    assert not [name for name in registry.names() if name.startswith("browser_")]

    off = Settings(harness_browser_enabled=False, harness_browser_allowed_hosts="127.0.0.1")
    assert off.browser_policy is None

    empty = Settings(harness_browser_enabled=True, harness_browser_allowed_hosts="")
    assert empty.browser_policy is None

    registry, closers = default_registry(browser_policy=policy())
    assert "browser_open" in registry.names()
    assert closers


def test_browser_tools_expose_the_seven_declared_names() -> None:
    sessions = BrowserSessions(policy())
    names = {tool.spec.name for tool in browser_tools(sessions, policy())}

    assert names == {
        "browser_open",
        "browser_read",
        "browser_click",
        "browser_fill",
        "browser_select",
        "browser_check",
        "browser_wait",
    }


def test_click_is_not_idempotent_and_allows_several_targets() -> None:
    sessions = BrowserSessions(policy())
    tools = {tool.spec.name: tool.spec for tool in browser_tools(sessions, policy())}

    click = tools["browser_click"]
    assert click.idempotent is False
    # 一次执行里要点多步（登录 → 进事项），不能只给 1 次；重复点同一个目标
    # 由"非幂等副作用指纹"拦住，不靠次数。
    assert click.call_limit == 8
    # 只读与填值可以重来；重来一次不会多写一份数据。
    for name in ("browser_open", "browser_read", "browser_fill", "browser_check"):
        assert tools[name].idempotent is True, name
    assert tools["browser_fill"].sensitive_fields == ["value"]


def test_unknown_argument_is_rejected_by_schema() -> None:
    registry, _ = default_registry(browser_policy=policy())
    grant = ToolGrant(registry=registry, allowed=["browser_open"])
    context = ToolContext(task_id=uuid4(), flow_id="f", step_id="s")

    record = grant.call(
        ToolCall(
            call_id="c1",
            name="browser_open",
            arguments={"url": "http://127.0.0.1:8101/login", "frame": "main"},
        ),
        context,
    )

    assert record.outcome is ToolOutcome.BLOCKED
    assert record.error_code == "invalid_arguments"


class _BoomTool:
    """被调用就炸：用来证明"未授权"是没执行，而不是执行后失败。"""

    spec = ToolSpec(
        name="browser_click",
        description="spy",
        parameters={"type": "object", "properties": {}},
    )

    def invoke(self, arguments: dict[str, Any], context: ToolContext) -> str:
        raise AssertionError("未授权的工具不该被执行")


def test_unauthorized_browser_tool_is_blocked_not_executed() -> None:
    registry = ToolRegistry()
    registry.register(_BoomTool())
    grant = ToolGrant(registry=registry, allowed=["browser_read"])
    context = ToolContext(task_id=uuid4(), flow_id="f", step_id="s")

    record = grant.call(
        ToolCall(call_id="c1", name="browser_click", arguments={}), context
    )

    assert record.outcome is ToolOutcome.BLOCKED
    assert record.error_code == "tool_not_authorized"


# ---------- 四、超时 = 结果未知，不重试 ----------


class _SleepyClickTool:
    """模拟一个"点下去之后页面半天没反应"的浏览器工具。"""

    def __init__(self) -> None:
        self.calls = 0
        self.spec = ToolSpec(
            name="browser_click",
            description="慢点击",
            parameters={"type": "object", "properties": {}},
            idempotent=False,
            timeout_seconds=30.0,
        )

    def invoke(self, arguments: dict[str, Any], context: ToolContext) -> str:
        self.calls += 1
        time.sleep(0.6)
        return "已点击"


def test_timeout_is_recorded_as_unknown_and_not_retried() -> None:
    """非幂等动作超时后**绝不能**自动重来——重来就是在赌它没生效。"""

    tool = _SleepyClickTool()
    registry = ToolRegistry()
    registry.register(tool)
    grant = ToolGrant(
        registry=registry,
        allowed=["browser_click"],
        call_timeout_seconds=0.2,
        max_attempts=3,
    )
    context = ToolContext(task_id=uuid4(), flow_id="f", step_id="s")

    record = grant.call(ToolCall(call_id="c1", name="browser_click", arguments={}), context)

    assert record.outcome is ToolOutcome.TIMEOUT
    assert record.error_code == "tool_timeout"
    assert record.attempts == 1
    assert tool.calls == 1
