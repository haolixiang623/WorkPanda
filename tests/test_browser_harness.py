"""真实浏览器回归：需要 chromium，靶子是仓库自带的 mock-site。

**跳过与失败的分界**（这是这类用例最容易静默失效的地方）：

- 本机没装 playwright / chromium → `skip`，跳过原因写进报告；
- CI 的浏览器 job 会设 `WORKPANDA_REQUIRE_BROWSER=1` → 缺浏览器是**失败**，
  而不是"跳过即通过"。job 配错（忘了装浏览器）必须在 CI 里红。

用例本身按 #79 的验收项写：只读路径、DOM 改版负例、超时、context 隔离、
跨站白名单、写操作被拒且没派发点击、失败现场脱敏落盘。
"""

from __future__ import annotations

import os
import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import uvicorn
from mock_site.app import build_apps
from mock_site.config import MockConfig
from mock_site.state import MockState

from workpanda.harness.browser import BrowserSessions, browser_tools, policy_from_hosts
from workpanda.harness.browser.policy import BrowserPolicy
from workpanda.harness.tools import ToolContext, ToolError

pytestmark = pytest.mark.browser

REQUIRE_BROWSER = os.environ.get("WORKPANDA_REQUIRE_BROWSER") == "1"

PILOT = (
    '[data-testid="service-item"]:has([data-testid="service-pilot"]) '
    '[data-testid="service-start"]'
)


def _unavailable_reason() -> str | None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return "未安装 playwright（pip install '.[browser]'）"
    try:
        with sync_playwright() as playwright:
            path = playwright.chromium.executable_path
    except Exception as exc:  # noqa: BLE001 - 原因要原样带给报告
        return f"chromium 不可用：{type(exc).__name__}: {exc}"
    if not path or not os.path.exists(path):
        return "chromium 未安装（playwright install chromium）"
    return None


_REASON = _unavailable_reason()
if _REASON is not None:
    if REQUIRE_BROWSER:
        raise RuntimeError(
            f"WORKPANDA_REQUIRE_BROWSER=1 要求浏览器用例可运行，但{_REASON}；"
            "缺浏览器时跳过等于把这条门禁关掉。"
        )
    pytest.skip(f"浏览器用例跳过：{_REASON}", allow_module_level=True)


# ---------- 靶子与夹具 ----------


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port: int = sock.getsockname()[1]
    sock.close()
    return port


@dataclass(frozen=True)
class Site:
    account_port: int
    apply_port: int
    state: MockState

    @property
    def account(self) -> str:
        return f"http://127.0.0.1:{self.account_port}"

    @property
    def apply(self) -> str:
        return f"http://127.0.0.1:{self.apply_port}"


@pytest.fixture(scope="module")
def site() -> Iterator[Site]:
    """两个端口、两个 origin 的真实 HTTP 服务。"""

    account_port, apply_port = _free_port(), _free_port()
    config = MockConfig(account_port=account_port, apply_port=apply_port)
    account_app, apply_app, state = build_apps(config)
    servers: list[uvicorn.Server] = []
    threads: list[threading.Thread] = []
    for app, port in ((account_app, account_port), (apply_app, apply_port)):
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        )
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        servers.append(server)
        threads.append(thread)
    try:
        for server in servers:
            deadline = time.monotonic() + 10
            while not server.started and time.monotonic() < deadline:
                time.sleep(0.02)
            assert server.started, "模拟站没起来"
        yield Site(account_port=account_port, apply_port=apply_port, state=state)
    finally:
        for server in servers:
            server.should_exit = True
        for thread in threads:
            thread.join(timeout=5)


@dataclass
class Env:
    """一份工具 + 一个任务上下文。每个用例一份，避免 cookie 互相污染。"""

    site: Site
    policy: BrowserPolicy
    sessions: BrowserSessions
    tools: dict[str, Any]
    context: ToolContext

    def call(self, name: str, **arguments: object) -> str:
        return str(self.tools[name].invoke(dict(arguments), self.context))

    def fails(self, name: str, **arguments: object) -> ToolError:
        with pytest.raises(ToolError) as captured:
            self.call(name, **arguments)
        return captured.value

    def url(self) -> str:
        return str(self.sessions.for_task(self.context.task_id).run(lambda page: page.url))


def make_env(
    site: Site,
    *,
    hosts: tuple[str, ...] | None = None,
    artifact_dir: Path | None = None,
    task_id: UUID | None = None,
    element_timeout_ms: int = 2000,
) -> Env:
    allowed = hosts or (f"127.0.0.1:{site.account_port}", f"127.0.0.1:{site.apply_port}")
    policy = policy_from_hosts(
        allowed,
        permitted_submit_testids=frozenset({"login-submit"}),
        element_timeout_ms=element_timeout_ms,
        artifact_dir=artifact_dir,
    )
    sessions = BrowserSessions(policy)
    tools = {tool.spec.name: tool for tool in browser_tools(sessions, policy)}
    return Env(
        site=site,
        policy=policy,
        sessions=sessions,
        tools=tools,
        context=ToolContext(
            task_id=task_id or uuid4(), flow_id="browser", step_id="step-1"
        ),
    )


@pytest.fixture
def env(site: Site) -> Iterator[Env]:
    built = make_env(site)
    try:
        yield built
    finally:
        built.sessions.close_all()


def login(env: Env) -> None:
    """走完整登录：账号、密码、验证码（验证码由站点的测试端点读出来）。"""

    env.call("browser_open", url=f"{env.site.account}/login")
    captcha_id = env.call("browser_read", selector='[data-testid="captcha-id"]').splitlines()[-1]
    code = httpx.get(
        f"{env.site.account}/__mock__/captcha/{captcha_id.strip()}", trust_env=False
    ).json()["code"]
    env.call("browser_fill", selector='[data-testid="input-username"]', value="demo")
    env.call("browser_fill", selector='[data-testid="input-password"]', value="demo1234")
    env.call("browser_fill", selector='[data-testid="input-captcha"]', value=code)
    env.call("browser_click", selector='[data-testid="login-submit"]')


# ---------- 一、正常路径 ----------


def test_readonly_path_reaches_the_fill_page(env: Env) -> None:
    """/login → /services → /apply：跨 origin 交接要真的走通。"""

    login(env)
    assert env.url() == f"{env.site.account}/services"
    current_user = env.call("browser_read", selector='[data-testid="current-user"]')
    assert current_user.splitlines()[-1] == "demo"

    env.call("browser_click", selector=PILOT)

    # 落地在**另一个 origin**：这条断言同时证明"跨站交接后仍在校验名单内"。
    assert env.url() == f"{env.site.apply}/apply/tab1"
    assert "办理事项：高龄补贴资格核验" in env.call("browser_read")

    env.call("browser_select", selector='[data-testid="field-domicile"]', value="local")
    env.call("browser_check", selector='[data-testid="field-situation-first"]')
    env.call("browser_fill", selector='[data-testid="field-phone"]', value="13800001234")
    assert env.call("browser_read", selector='[data-testid="field-phone"]').endswith(
        "13800001234"
    )
    assert "已选中" in env.call("browser_read", selector='[data-testid="field-situation-first"]')


def test_ambiguous_selector_is_refused_instead_of_picking_the_first(env: Env) -> None:
    """列表页上 `service-start` 有 5 个：点错行比失败严重。"""

    login(env)

    refused = env.fails("browser_click", selector='[data-testid="service-start"]')
    assert refused.code == "ambiguous_target"
    assert "5 个" in refused.message
    # 被拒之后页面没动过。
    assert env.url() == f"{env.site.account}/services"

    env.call("browser_click", selector='[data-testid="service-start"]', index=2)
    assert env.url() == f"{env.site.apply}/apply/tab1"


# ---------- 二、失败路径：明确失败码，不误报成功 ----------


def test_page_revision_is_an_explicit_failure(env: Env) -> None:
    login(env)

    missing = env.fails("browser_click", selector='[data-testid="field-phone"]')
    assert missing.code == "element_not_found"
    assert "改版" in missing.message

    timeout = env.fails("browser_wait", selector='[data-testid="submit-application"]')
    assert timeout.code == "element_timeout"


def test_submit_click_is_refused_before_anything_is_dispatched(env: Env) -> None:
    """写操作拒绝必须发生在派发点击之前：页面状态不能有半点变化。"""

    login(env)
    env.call("browser_click", selector=PILOT)
    env.call("browser_fill", selector='[data-testid="field-phone"]', value="13800001234")
    before = env.url()

    for selector in ('[data-testid="basic-submit"]',):
        refused = env.fails("browser_click", selector=selector)
        assert refused.code == "submit_click_blocked"
        assert "HARNESS_BROWSER_PERMITTED_CLICK_TESTIDS" in refused.message

    # 没跳走、也没被服务端接收（tab2 的地址不会出现）。
    assert env.url() == before
    assert "材料上传" not in env.call("browser_read", selector='[data-testid="basic-form"]')


# ---------- 三、白名单：跳转后的站点也要显式列出 ----------


def test_second_origin_must_be_whitelisted(site: Site) -> None:
    """只列账号站时，点「去办理」跳出白名单 → 拒绝并销毁会话。"""

    built = make_env(site, hosts=(f"127.0.0.1:{site.account_port}",))
    try:
        session = built.sessions.for_task(built.context.task_id)
        login(built)
        refused = built.fails("browser_click", selector=PILOT)
        assert refused.code == "redirect_host_not_allowed"
        assert f"127.0.0.1:{site.apply_port}" in refused.message
        # 会话已被销毁，不会留一个在白名单外跑着的页面。
        assert session.closed is True
    finally:
        built.sessions.close_all()


def test_off_whitelist_host_is_never_opened(env: Env) -> None:
    refused = env.fails("browser_open", url="http://127.0.0.1:9/login")

    assert refused.code == "host_not_allowed"


# ---------- 四、隔离：两个任务不共享 cookie ----------


def test_two_tasks_do_not_share_cookies(site: Site) -> None:
    first = make_env(site)
    second = make_env(site)
    try:
        login(first)
        assert first.call(
            "browser_read", selector='[data-testid="current-user"]'
        ).splitlines()[-1] == "demo"

        # 同一进程、同一站点，另一份 context 没有那份 cookie。
        report = second.call("browser_open", url=f"{site.account}/services")
        assert "session_required" in report
        assert "当前登录" not in report

        sessions = first.sessions
        assert sessions.for_task(first.context.task_id) is not sessions.for_task(
            uuid4()
        )
    finally:
        first.sessions.close_all()
        second.sessions.close_all()


# ---------- 五、失败现场：默认不留，留则脱敏 ----------


def test_failure_artifacts_are_opt_in_and_redacted(site: Site, workdir) -> None:
    built = make_env(site, artifact_dir=workdir / "artifacts")
    try:
        login(built)
        built.call("browser_click", selector=PILOT)
        built.call("browser_fill", selector='[data-testid="field-phone"]', value="13800001234")
        failure = built.fails("browser_wait", selector='[data-testid="missing-node"]')
        assert failure.code == "element_timeout"
        assert "现场已留档" in failure.message

        files = sorted((workdir / "artifacts").rglob("*"))
        html_files = [item for item in files if item.suffix == ".html"]
        png_files = [item for item in files if item.suffix == ".png"]
        assert html_files and png_files
        dumped = html_files[0].read_text(encoding="utf-8")
        # 填进去的手机号不允许出现在磁盘上（截图另走遮罩，见 artifacts 的说明）。
        assert "13800001234" not in dumped
        assert "demo1234" not in dumped
        assert oct(html_files[0].stat().st_mode & 0o777) == "0o600"
    finally:
        built.sessions.close_all()


def test_no_artifact_is_written_by_default(site: Site, workdir) -> None:
    built = make_env(site)
    try:
        login(built)
        failure = built.fails("browser_wait", selector='[data-testid="missing-node"]')
        assert "现场已留档" not in failure.message
        assert not list(workdir.rglob("*"))
    finally:
        built.sessions.close_all()
