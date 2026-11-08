"""「不应发生」的清单：边界、不联网、必需的定位点、能分别启动。

这些断言不测业务行为，测的是模拟站最容易悄悄腐坏的地方——某天有人为了图方便
`import workpanda`，或者为了好看加一个 CDN 字体，或者把某个 `data-testid` 改名。
它们失败时不会报「功能不对」，但会让 P2/P3 的测试变成对着一个不一样的靶子开枪。
"""

from __future__ import annotations

import ast
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from mock_site.account_site import create_account_app
from mock_site.apply_site import create_apply_app
from mock_site.config import MockConfig
from mock_site.state import MockState
from support import ACCOUNT_ORIGIN, APPLY_ORIGIN, enter_pilot_service, fill_basic, two_files

MOCK_SITE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = MOCK_SITE_ROOT.parent
PACKAGE_ROOT = MOCK_SITE_ROOT / "mock_site"
ALLOWED_THIRD_PARTY = {"fastapi", "starlette", "uvicorn"}
FORBIDDEN_OUTBOUND = {"aiohttp", "httpx", "requests", "urllib3"}

TESTID_PATTERN = re.compile(r'data-testid="([^"]+)"')

REQUIRED_TESTIDS = {
    "login": {"login-form", "input-username", "input-password", "input-captcha",
              "captcha-image", "captcha-id", "login-submit"},
    "qr": {"qr-image", "qr-ticket", "qr-status"},
    "services": {"service-item", "service-pilot", "service-start", "current-user"},
    "basic": {"field-domicile", "field-situation-first", "field-situation-change",
              "field-situation-reissue", "field-phone", "field-address", "basic-submit"},
    "materials": {"upload-input", "upload-submit", "attachment-count",
                  "attachment-limits", "upload-form"},
    "confirm": {"summary-service", "summary-domicile", "summary-situations",
                "summary-phone", "summary-address", "summary-attachments",
                "submit-application"},
    "receipt": {"receipt-no", "receipt-status", "receipt-service"},
    "error": {"error-page", "error-code", "error-message"},
}


def module_import_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def test_mock_site_never_imports_the_product() -> None:
    for path in sorted(PACKAGE_ROOT.glob("*.py")):
        roots = module_import_roots(path)
        assert "workpanda" not in roots, f"{path.name} 引用了产品代码"


def test_mock_site_imports_only_stdlib_and_web_framework() -> None:
    for path in sorted(PACKAGE_ROOT.glob("*.py")):
        unknown = module_import_roots(path) - set(sys.stdlib_module_names) - ALLOWED_THIRD_PARTY
        assert unknown <= {"mock_site"}, f"{path.name} 引入了额外依赖：{sorted(unknown)}"


def test_mock_site_has_no_outbound_http_client() -> None:
    for path in sorted(PACKAGE_ROOT.glob("*.py")):
        roots = module_import_roots(path)
        assert not roots & FORBIDDEN_OUTBOUND, f"{path.name} 引入了出站 HTTP 客户端：{roots}"
        source = path.read_text(encoding="utf-8")
        assert "urllib.request" not in source
        assert "urlopen(" not in source


def test_socket_is_only_used_for_port_probing() -> None:
    users = {
        path.name for path in PACKAGE_ROOT.glob("*.py") if "socket" in module_import_roots(path)
    }
    assert users == {"__main__.py"}, f"只有启动脚本可以碰 socket，实际：{users}"


def test_mock_site_never_calls_request_form() -> None:
    """`request.form()` 会把 `python-multipart` 变成隐式硬依赖。

    Starlette 的 `Request.form()` 一进函数就断言 `python-multipart` 装了，哪怕这次
    请求只是 urlencoded。本地装了看不出来，CI 上没装就是 500 —— 所以这里从语法树
    上禁掉：urlencoded 走 `shared.form_body`，multipart 走 `mock_site.multipart`。
    """

    for path in sorted(PACKAGE_ROOT.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        offenders = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "form"
        ]
        assert not offenders, (
            f"{path.name} 在第 {offenders} 行调用了 .form()："
            "它会隐式依赖 python-multipart，改用 shared.form_body"
        )


def test_product_code_never_imports_the_mock_site() -> None:
    for path in sorted((REPO_ROOT / "src" / "workpanda").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        assert "mock_site" not in source, f"{path} 引用了模拟站"


def test_each_site_can_be_built_on_its_own() -> None:
    account = create_account_app(MockState(MockConfig()))
    apply_app = create_apply_app(MockState(MockConfig()))
    assert account.routes and apply_app.routes
    # 两份 app 各自带自己的文档页关闭设置；它们不共享路由表。
    account_paths = {getattr(route, "path", "") for route in account.routes}
    apply_paths = {getattr(route, "path", "") for route in apply_app.routes}
    assert "/login" in account_paths
    assert "/login" not in apply_paths
    assert "/apply/tab1" in apply_paths


def test_readme_says_this_is_a_mock() -> None:
    readme = (MOCK_SITE_ROOT / "README.md").read_text(encoding="utf-8")
    head = readme.split("\n## ", 1)[0]
    assert "模拟站" in head
    assert "不代表任何真实政务系统" in head


def test_no_page_references_an_external_resource(onsite_pages: dict[str, str]) -> None:
    for name, html in onsite_pages.items():
        assert "https://" not in html, f"{name} 页面出现 https 外链"
        assert 'src="//' not in html and 'href="//' not in html
        assert "@import" not in html
        for url in re.findall(r"http://[^\"'\s>]+", html):
            assert url.startswith((ACCOUNT_ORIGIN, APPLY_ORIGIN)), f"{name} 页面外链到 {url}"


def test_every_page_carries_the_mock_banner(onsite_pages: dict[str, str]) -> None:
    for name, html in onsite_pages.items():
        assert 'data-testid="mock-banner"' in html, f"{name} 页面没有模拟站提示条"


def test_required_testids_are_present(onsite_pages: dict[str, str]) -> None:
    for name, required in REQUIRED_TESTIDS.items():
        present = set(TESTID_PATTERN.findall(onsite_pages[name]))
        missing = required - present
        assert not missing, f"{name} 页缺定位点：{sorted(missing)}"


# --------------------------------------------------------------------- 助手与夹具


@pytest.fixture
async def onsite_pages(browser: httpx.AsyncClient) -> dict[str, str]:
    """把八类页面各抓一份：登录、扫码、列表、三个 tab、回执、错误页。"""

    pages: dict[str, str] = {}
    pages["login"] = (await browser.get(f"{ACCOUNT_ORIGIN}/login")).text
    pages["qr"] = (await browser.get(f"{ACCOUNT_ORIGIN}/login/qr")).text

    landed = await enter_pilot_service(browser)
    pages["basic"] = landed.text
    pages["services"] = (await browser.get(f"{ACCOUNT_ORIGIN}/services")).text
    pages["materials"] = (await browser.get(f"{APPLY_ORIGIN}/apply/tab2")).text
    pages["confirm"] = (await browser.get(f"{APPLY_ORIGIN}/apply/tab3")).text

    await fill_basic(browser)
    await browser.post(f"{APPLY_ORIGIN}/apply/tab2/upload", files=two_files(2))
    receipt = await browser.post(f"{APPLY_ORIGIN}/apply/submit")
    pages["receipt"] = receipt.text
    pages["error"] = (
        await browser.get(f"{APPLY_ORIGIN}/receipt/MOCK-00000000-9999")
    ).text
    return pages


def free_ports(count: int) -> list[int]:
    """先绑 0 号端口拿到号码再全部释放：让 CLI 有真实可用的端口。"""

    sockets = []
    try:
        for _ in range(count):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", 0))
            sockets.append(sock)
        return [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


def run_cli(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "mock_site", *args],
        cwd=MOCK_SITE_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )


def test_cli_check_succeeds_with_two_free_ports() -> None:
    account_port, apply_port = free_ports(2)
    completed = run_cli(
        "--check", "--account-port", str(account_port), "--apply-port", str(apply_port)
    )
    assert completed.returncode == 0, completed.stderr
    assert f"127.0.0.1:{account_port}" in completed.stdout
    assert f"127.0.0.1:{apply_port}" in completed.stdout


def test_cli_refuses_a_non_loopback_bind() -> None:
    env = os.environ.copy()
    env["MOCK_SITE_BIND_HOST"] = "0.0.0.0"
    completed = run_cli("--check", env=env)
    assert completed.returncode == 2
    assert "回环" in completed.stderr


def test_cli_actually_serves_both_ports() -> None:
    """`--check` 只证明端口能绑；这一条证明 `python -m mock_site` 真能起来服务。"""

    account_port, apply_port = free_ports(2)
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "mock_site",
            "--account-port",
            str(account_port),
            "--apply-port",
            str(apply_port),
        ],
        cwd=MOCK_SITE_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    account_origin = f"http://127.0.0.1:{account_port}"
    apply_origin = f"http://127.0.0.1:{apply_port}"
    try:
        deadline = time.monotonic() + 15
        with httpx.Client(timeout=2.0, trust_env=False) as client:
            while True:
                try:
                    account = client.get(f"{account_origin}/healthz").json()
                    apply_site = client.get(f"{apply_origin}/healthz").json()
                except httpx.HTTPError:
                    account = apply_site = {}
                if account.get("site") == "account" and apply_site.get("site") == "apply":
                    break
                if time.monotonic() > deadline:
                    raise AssertionError("python -m mock_site 没有在 15 秒内起来")
                time.sleep(0.1)
            page = client.get(f"{account_origin}/login")
            assert page.status_code == 200
            assert 'data-testid="login-form"' in page.text
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def test_cli_reports_a_busy_port() -> None:
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    holder.bind(("127.0.0.1", 0))
    holder.listen(8)
    try:
        completed = run_cli("--check", "--account-port", str(holder.getsockname()[1]))
    finally:
        holder.close()
    assert completed.returncode == 3
    assert "不可用" in completed.stderr
