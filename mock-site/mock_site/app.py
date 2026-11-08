"""装配：一个进程、两份应用、两个端口、一份共享状态。

两个 app 必须真的各监听一个端口，否则「跨 origin」就是句空话：同端口不同路径的
跳转不会带上网关白名单、cookie 作用域、per-context 隔离这些真问题。
"""

from __future__ import annotations

import time
from collections.abc import Callable

from fastapi import FastAPI

from mock_site.account_site import create_account_app
from mock_site.apply_site import create_apply_app
from mock_site.config import MockConfig
from mock_site.state import MockState


def build_apps(
    config: MockConfig | None = None,
    *,
    clock: Callable[[], float] = time.time,
) -> tuple[FastAPI, FastAPI, MockState]:
    """返回 (账号站, 办理站, 共享状态)。`clock` 可注入，用于精确模拟过期。"""

    resolved = config or MockConfig.from_env()
    state = MockState(resolved, clock=clock)
    return (
        create_account_app(state, resolved),
        create_apply_app(state, resolved),
        state,
    )
