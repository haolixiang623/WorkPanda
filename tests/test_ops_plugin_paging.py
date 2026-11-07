"""运维插件的翻页管道：游标能回传、能被送回去，且不会变出新的网络去向。

插件不在包内（目录名带连字符），因此按文件路径加载。这里只替换最外层的
`_fetch`，被测的是"参数怎么攒、游标怎么回"这一段，不碰真实网络。
"""

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

PLUGIN_PATH = Path(__file__).resolve().parent.parent / "plugins" / "workpanda-ops" / "main.py"
ALLOWED_CURSOR_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_=")


@pytest.fixture
def plugin(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    spec = importlib.util.spec_from_file_location("workpanda_ops_plugin", PLUGIN_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    calls: list[tuple[str, dict]] = []

    def fake_fetch(path: str, params: dict) -> tuple[object, str]:
        calls.append((path, params))
        return [{"task_id": "t1"}], "next-token"

    monkeypatch.setattr(module, "_fetch", fake_fetch)
    monkeypatch.setenv("WORKPANDA_OPS_TOKEN", "token-for-tests")
    module.recorded = calls  # type: ignore[attr-defined]
    return module


async def test_tasks_tool_surfaces_the_next_cursor(plugin: ModuleType) -> None:
    payload = json.loads(await plugin.workpanda_ops_tasks(limit=5))

    assert payload["data"]["next_cursor"] == "next-token"
    # 光给数据不给"还有下一页"的提示，模型会以为这就是全部。
    assert "下一页" in payload["text"]


async def test_tasks_tool_sends_a_sanitized_cursor_back(plugin: ModuleType) -> None:
    await plugin.workpanda_ops_tasks(cursor="http://evil.example/x?a=1")

    path, params = plugin.recorded[-1]  # type: ignore[attr-defined]
    # 路径只能是代码里的常量：调用方给什么都改不了请求去哪。
    assert path == "/api/ops/tasks"
    assert set(params["cursor"]) <= ALLOWED_CURSOR_CHARS
    assert ":" not in params["cursor"] and "/" not in params["cursor"]


async def test_traces_tool_passes_the_cursor_through(plugin: ModuleType) -> None:
    payload = json.loads(await plugin.workpanda_ops_traces(cursor="abc-def"))

    path, params = plugin.recorded[-1]  # type: ignore[attr-defined]
    assert path == "/api/ops/traces"
    assert params["cursor"] == "abc-def"
    assert payload["data"]["next_cursor"] == "next-token"


async def test_audit_tool_supports_kind_and_paging(plugin: ModuleType) -> None:
    payload = json.loads(await plugin.workpanda_ops_audit(kind="effect", cursor="abc"))

    path, params = plugin.recorded[-1]  # type: ignore[attr-defined]
    assert path == "/api/ops/audit"
    assert params["kind"] == "effect"
    assert params["cursor"] == "abc"
    assert payload["data"]["next_cursor"] == "next-token"


async def test_tools_remain_read_only(plugin: ModuleType) -> None:
    """翻页只加一个查询参数，不引入任何写入口。"""

    for call in (
        plugin.workpanda_ops_tasks(cursor="abc"),
        plugin.workpanda_ops_traces(cursor="abc"),
        plugin.workpanda_ops_audit(cursor="abc"),
    ):
        await call

    assert [path for path, _ in plugin.recorded] == [  # type: ignore[attr-defined]
        "/api/ops/tasks",
        "/api/ops/traces",
        "/api/ops/audit",
    ]


async def test_failure_envelope_never_leaks_the_token(
    plugin: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(path: str, params: dict) -> tuple[object, str]:
        raise RuntimeError("运维接口返回 HTTP 401")

    monkeypatch.setattr(plugin, "_fetch", boom)
    payload = json.loads(await plugin.workpanda_ops_tasks())

    assert "401" in payload["text"]
    assert "token-for-tests" not in payload["text"]
