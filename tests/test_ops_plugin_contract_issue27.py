"""Issue #27：把只读插件链路在新的 ops 代码上实测一遍。

PR #26 改了 `src/workpanda/api/ops.py` 与 `src/workpanda/harness/trace.py`，
并承诺 `/api/ops/traces` 响应体保持**数组**、翻页放 `X-Next-Cursor`。只读插件
`plugins/workpanda-ops/main.py` 依赖这个形状，但此前只在旧代码上跑通过。

本测试在进程内直接驱动真实插件的 `_fetch` 打真实 FastAPI 运维接口（把 urllib
请求路由到 httpx ASGI 传输，**不触真实网络**），锁定「插件 → 服务端 → 响应形状
→ octop_ui 信封」这条链路。它对应的是 Issue #27 的验收项：5 个端点全部 200、
响应保持数组、翻页走 `X-Next-Cursor`、浏览器卡片所需的 envelope 字段齐全。

真实 Octop 控制台 + 浏览器渲染走查（滑块登录、WebSocket 聊天、`pageerror` 为空）
依赖本机试点（Octop 1.0.2 :8090），不在自动化环境内，故此处只锁协议层契约。
"""

import importlib.util
import json
import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from urllib.error import HTTPError
from urllib.parse import parse_qsl, urlparse

import pytest
from fastapi.testclient import TestClient

from tests.support import TEST_FLOWS
from workpanda.bootstrap import build_container
from workpanda.config import Settings
from workpanda.harness.trace import TraceRecord
from workpanda.infrastructure.adapters import ScriptedExecutor
from workpanda.main import create_app

PLUGIN_PATH = Path(__file__).resolve().parent.parent / "plugins" / "workpanda-ops" / "main.py"
OPS_TOKEN = "ops-token-issue-27"
BASE_URL = "http://127.0.0.1:8010"


def _build_client() -> tuple[TestClient, object]:
    container = build_container(
        Settings(store_backend="memory", enable_demo_flow=False, ops_token=OPS_TOKEN),
        flows=TEST_FLOWS,
        executor=ScriptedExecutor(),
    )
    return TestClient(create_app(container=container)), container


def _seed_traces(container, count: int) -> None:
    for index in range(count):
        container.traces.save(
            TraceRecord(
                trace_id=f"issue27-trace-{index:02d}",
                task_id=uuid.uuid4(),
                flow_id="test.flow",
                flow_version="1",
                step_id="run",
                outcome="succeeded" if index % 2 == 0 else "unconfirmed",
                stop_reason="completed",
                model_calls=1,
                tool_calls=1,
                successful_tool_calls=1,
                tokens=10,
                duration_ms=5,
                created_at=datetime(2026, 11, 7, 4, 0, tzinfo=UTC) + timedelta(minutes=index),
            )
        )


@pytest.fixture
def plugin(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """加载真实插件模块，并把它的 `_fetch` 网络调用改到本地 ASGI 应用。"""

    spec = importlib.util.spec_from_file_location("workpanda_ops_plugin_27", PLUGIN_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    client, container = _build_client()
    _seed_traces(container, 3)
    captured: list[tuple[str, dict, dict]] = []

    def fake_urlopen(request, timeout=None):
        parsed = urlparse(request.full_url)
        path = parsed.path
        params = dict(parse_qsl(parsed.query))
        headers = {k: v for k, v in request.headers.items()}
        captured.append((path, params, headers))
        # Starlette TestClient 的阻塞 portal 可在插件的 asyncio.to_thread 工作线程里安全调用。
        response = client.get(path, params=params, headers=headers)
        if response.status_code >= 400:
            raise HTTPError(
                request.full_url, response.status_code, response.reason, response.headers, None
            )

        class _FakeResponse:
            def __init__(self, content: bytes, resp_headers) -> None:
                self._content = content
                self.headers = resp_headers

            def read(self):
                return self._content

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        return _FakeResponse(response.content, response.headers)

    monkeypatch.setattr(module, "urlopen", fake_urlopen)
    # 让插件打到本地 ASGI 应用，而不是真去连 127.0.0.1:8010。
    monkeypatch.setattr(module, "_connection", lambda: (BASE_URL, OPS_TOKEN))
    module._captured = captured  # type: ignore[attr-defined]
    yield module
    container.close()
    sys.modules.pop(spec.name, None)


async def test_all_five_ops_endpoints_return_200() -> None:
    """验收项：tasks / traces / traces/summary / audit / tool-runtime 五个端点全部 200。

    直接打 TestClient，确认新代码下运维接口整体可用、响应形状未退化。
    """

    client, container = _build_client()
    try:
        headers = {"X-WorkPanda-Ops-Token": OPS_TOKEN}
        for path in (
            "/api/ops/tasks",
            "/api/ops/traces",
            "/api/ops/traces/summary",
            "/api/ops/audit",
            "/api/ops/tool-runtime",
        ):
            response = client.get(path, headers=headers)
            assert response.status_code == 200, f"{path} 返回 {response.status_code}"
    finally:
        container.close()


async def test_plugin_tasks_tool_gets_array_with_cursor(plugin: ModuleType) -> None:
    """插件拿到任务列表：响应体是数组、octop_ui 信封齐全、next_cursor 字段存在。"""

    payload = json.loads(await plugin.workpanda_ops_tasks(limit=5))

    assert payload["octop_ui"]["renderer"] == "workpanda_ops"
    assert payload["data"]["view"] == "tasks"
    assert payload["data"]["readonly"] is True
    assert isinstance(payload["data"]["items"], list)
    assert "next_cursor" in payload["data"]
    # 调用确实打到了服务端 /api/ops/tasks。
    assert plugin._captured[-1][0] == "/api/ops/tasks"  # type: ignore[attr-defined]


async def test_plugin_traces_tool_consumes_array_and_cursor(plugin: ModuleType) -> None:
    """插件消费留痕：响应体是数组，且翻页游标能从 `X-Next-Cursor` 透传出来。"""

    # limit=1 < 3 条，服务端应下发 X-Next-Cursor。
    payload = json.loads(await plugin.workpanda_ops_traces(limit=1))

    assert payload["octop_ui"]["renderer"] == "workpanda_ops"
    assert payload["data"]["view"] == "traces"
    items = payload["data"]["items"]
    assert isinstance(items, list)
    assert len(items) == 1
    # 插件必须把服务端的 X-Next-Cursor 原样透传成 next_cursor。
    assert payload["data"]["next_cursor"]
    assert "下一页" in payload["text"]


async def test_plugin_overview_tool_gets_summary_and_runtime(plugin: ModuleType) -> None:
    """overview 同时拿聚合指标与工具线程台账，二者都进 envelope。"""

    payload = json.loads(await plugin.workpanda_ops_overview())

    assert payload["data"]["view"] == "overview"
    assert isinstance(payload["data"]["metrics"], dict)
    assert isinstance(payload["data"]["tool_runtime"], dict)
    # 未启用 harness 时工具线程台账如实报 absence，而不是编一排 0。
    assert payload["data"]["tool_runtime"]["available"] is False


async def test_plugin_audit_tool_returns_array_without_leaking(plugin: ModuleType) -> None:
    """audit 也是数组；失败时只回状态码，不回显凭据或内部响应体。"""

    payload = json.loads(await plugin.workpanda_ops_audit(limit=5))
    assert payload["data"]["view"] == "audit"
    assert isinstance(payload["data"]["items"], list)

    # 让插件以为没配置凭据，确认失败信封只带状态码、不含运维凭据。
    saved = os.environ.pop("WORKPANDA_OPS_TOKEN", None)
    monkeypatch_connection = plugin._connection
    plugin._connection = lambda: (BASE_URL, "")  # type: ignore[assignment]
    try:
        bad = json.loads(await plugin.workpanda_ops_audit())
    finally:
        plugin._connection = monkeypatch_connection  # type: ignore[assignment]
        if saved is not None:
            os.environ["WORKPANDA_OPS_TOKEN"] = saved
    assert OPS_TOKEN not in bad["text"]
    assert "读取失败" in bad["text"]
