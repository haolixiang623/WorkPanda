"""测试共用的辅助件：可控时钟、双 origin 传输、页面操作。

放在 `support.py` 而不是 `conftest.py`，是因为用例需要直接调用它们；`conftest.py`
只负责夹具装配。pytest 用 prepend 模式导入测试模块，`mock-site/tests` 会自动进入
`sys.path`，所以这里不需要任何路径魔法。
"""

from __future__ import annotations

import re

import httpx

ACCOUNT_ORIGIN = "http://127.0.0.1:8101"
APPLY_ORIGIN = "http://127.0.0.1:8102"
CAPTCHA_ID_PATTERN = re.compile(r'name="captcha_id" value="([^"]+)"')


class FakeClock:
    """可控时钟：模拟站里所有 TTL 都读它，测试不必真的等 90 秒。"""

    def __init__(self, start: float = 1_760_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class DualOriginTransport(httpx.AsyncBaseTransport):
    """按端口把请求分给两个 ASGI app。

    这不是「绕开跨站」：浏览器里 cookie 也不按端口隔离，`127.0.0.1:8101` 种下的
    cookie 一样会带去 `127.0.0.1:8102`。把这条真实行为留在测试里，P2 才会撞上它。
    """

    def __init__(self, transports: dict[int, httpx.AsyncBaseTransport]) -> None:
        self._transports = transports

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        transport = self._transports.get(request.url.port or 0)
        if transport is None:
            raise httpx.ConnectError(
                f"没有为端口 {request.url.port} 注册站点", request=request
            )
        return await transport.handle_async_request(request)

    async def aclose(self) -> None:
        for transport in self._transports.values():
            await transport.aclose()


async def fetch_captcha(client: httpx.AsyncClient) -> tuple[str, str]:
    page = await client.get("/login")
    match = CAPTCHA_ID_PATTERN.search(page.text)
    assert match is not None, "登录页没有 captcha_id"
    captcha_id = match.group(1)
    answer = await client.get(f"/__mock__/captcha/{captcha_id}")
    return captcha_id, str(answer.json()["code"])


async def login(
    client: httpx.AsyncClient, *, username: str = "demo", password: str = "demo1234"
) -> httpx.Response:
    captcha_id, code = await fetch_captcha(client)
    return await client.post(
        "/login",
        data={
            "username": username,
            "password": password,
            "captcha_id": captcha_id,
            "captcha_code": code,
        },
    )


async def login_wrong_captcha(
    client: httpx.AsyncClient, *, username: str = "demo"
) -> httpx.Response:
    captcha_id, _code = await fetch_captcha(client)
    return await client.post(
        "/login",
        data={
            "username": username,
            "password": "demo1234",
            "captcha_id": captcha_id,
            "captcha_code": "ZZZZ",
        },
    )


async def enter_pilot_service(client: httpx.AsyncClient) -> httpx.Response:
    """登录 → 事项列表 → 点试点事项的「去办理」→ 落在另一个 origin 的 tab1。"""

    await login(client)
    services = await client.get(f"{ACCOUNT_ORIGIN}/services")
    assert "service-pilot" in services.text
    return await client.get(f"{ACCOUNT_ORIGIN}/handoff/elderly-subsidy")


async def fill_basic(client: httpx.AsyncClient, **overrides: object) -> httpx.Response:
    data: dict[str, object] = {
        "domicile_type": "local",
        "situations": ["first"],
        "phone": "13800001111",
        "address": "苏州市姑苏区示例路 18 号",
    }
    data.update(overrides)
    return await client.post(f"{APPLY_ORIGIN}/apply/tab1", data=data)


def two_files(count: int = 2) -> list[tuple[str, tuple[str, bytes, str]]]:
    names = ("户主页", "身份证", "申请表", "证明材料", "补充说明")
    return [
        (
            "files",
            (
                f"{names[index % len(names)]}.pdf",
                b"%PDF-1.4 mock" + bytes([index]),
                "application/pdf",
            ),
        )
        for index in range(count)
    ]


async def upload_ready_files(client: httpx.AsyncClient) -> httpx.Response:
    return await client.post(f"{APPLY_ORIGIN}/apply/tab2/upload", files=two_files(2))
