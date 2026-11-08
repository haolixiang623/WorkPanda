"""mock-site 的测试夹具：两个 origin、一个 cookie jar、一个可控时钟。"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from mock_site.app import build_apps
from mock_site.config import MockConfig
from mock_site.state import MockState
from support import ACCOUNT_ORIGIN, DualOriginTransport, FakeClock


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def config() -> MockConfig:
    return MockConfig()


@pytest.fixture
def apps(config: MockConfig, clock: FakeClock):
    return build_apps(config, clock=clock)


@pytest.fixture
def state(apps) -> MockState:
    return apps[2]


def _transport(apps) -> DualOriginTransport:
    account, apply_app, _state = apps
    return DualOriginTransport(
        {
            8101: httpx.ASGITransport(account),
            8102: httpx.ASGITransport(apply_app),
        }
    )


@pytest.fixture
async def browser(apps) -> AsyncIterator[httpx.AsyncClient]:
    """会跟随重定向的客户端，行为最接近浏览器。"""

    async with httpx.AsyncClient(
        transport=_transport(apps), base_url=ACCOUNT_ORIGIN, follow_redirects=True
    ) as client:
        yield client


@pytest.fixture
async def probe(apps) -> AsyncIterator[httpx.AsyncClient]:
    """不跟随重定向，用来看清每一步的状态码与 Location。"""

    async with httpx.AsyncClient(
        transport=_transport(apps), base_url=ACCOUNT_ORIGIN, follow_redirects=False
    ) as client:
        yield client


@pytest.fixture
async def stranger(apps) -> AsyncIterator[httpx.AsyncClient]:
    """客户端地址不是回环：`/__mock__/*` 必须拒绝它。"""

    transport = httpx.ASGITransport(apps[0], client=("203.0.113.9", 51234))
    async with httpx.AsyncClient(transport=transport, base_url=ACCOUNT_ORIGIN) as client:
        yield client
