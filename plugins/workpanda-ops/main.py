"""WorkPanda 运维只读插件（Octop ``kind: tool``）。

设计边界（与 docs/OCTOP-INTEGRATION.md 一致）：

- **只读**：只发 ``GET``，且路径全部是本文件内的常量，不接受调用方传入 URL；
  不提供改状态、重试执行、取消任务等任何写入口。
- **凭据隔离**：``WORKPANDA_OPS_TOKEN`` 单独下发，不复用模型 API Key，
  不硬编码、不写进返回内容或错误信息。
- **数据最小化**：只透传白名单字段，字符串截断；不展示会话标识、
  失败原文、槽位内容等可能含用户数据的字段。
- **零第三方依赖**：仅用标准库，安装插件时不触发 pip 安装。

工具返回 Octop 的 ``octop_ui`` envelope，由 ``ui/dist/index.js`` 渲染。
"""

from __future__ import annotations

import asyncio
import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

DEFAULT_BASE_URL = "http://127.0.0.1:8010"
TIMEOUT_SECONDS = 8.0
MAX_LIMIT = 100
MAX_TEXT = 200

RENDERER = "workpanda_ops"
UI_TOOLS = (
    "workpanda_ops_overview",
    "workpanda_ops_tasks",
    "workpanda_ops_traces",
    "workpanda_ops_audit",
)

TASK_FIELDS = (
    "task_id", "flow_id", "flow_version", "status", "current_step",
    "failure_code", "created_at", "updated_at",
)
TRACE_FIELDS = (
    "trace_id", "task_id", "flow_id", "step_id", "outcome", "stop_reason",
    "error_code", "risk", "model_calls", "tool_calls", "successful_tool_calls",
    "blocked_tool_calls", "failed_tool_calls", "timed_out_tool_calls",
    "duration_ms", "tokens_total",
)
SUMMARY_FIELDS = (
    "total", "succeeded", "failed", "unconfirmed", "unconfirmed_rate",
    "tool_calls", "blocked_tool_calls", "failed_tool_calls",
    "timed_out_tool_calls", "skipped_tool_calls", "avg_model_calls",
    "avg_duration_ms", "tokens_total",
)
RUNTIME_FIELDS = ("available", "live", "stuck", "max_stuck")
AUDIT_FIELDS = (
    "entry_id", "task_id", "flow_id", "step_id", "kind", "effect",
    "outcome", "risk", "requires_manual_check", "created_at",
)

# 控制台可配置（Octop 的 config_fields）；环境变量作为无控制台配置时的兜底。
CONFIG_FIELDS = [
    {"name": "ops_base_url", "type": "text", "required": False},
    {"name": "ops_token", "type": "text", "required": False},
]


def _plugin_config() -> dict:
    """读取 Octop 控制台为该插件保存的配置；SDK 不可用时静默降级。"""

    try:
        from octop_harness.plugins import get_tool_config  # type: ignore[import-not-found]

        for tool_name in UI_TOOLS:
            cfg = get_tool_config(tool_name)
            if isinstance(cfg, dict) and cfg:
                return cfg
    except Exception:
        pass
    return {}


def _connection() -> tuple[str, str]:
    cfg = _plugin_config()
    base = str(
        cfg.get("ops_base_url")
        or os.environ.get("WORKPANDA_OPS_BASE_URL")
        or DEFAULT_BASE_URL
    ).rstrip("/")
    token = str(cfg.get("ops_token") or os.environ.get("WORKPANDA_OPS_TOKEN") or "")
    return base, token


def _get(path: str, params: dict) -> object:
    """只读 GET。path 是代码内常量，参数来自白名单，避免 SSRF 与注入。"""

    base, token = _connection()
    if not token:
        raise RuntimeError("未配置运维凭据 ops_token / WORKPANDA_OPS_TOKEN")
    query = urlencode({k: v for k, v in params.items() if v not in (None, "")})
    url = f"{base}{path}" + (f"?{query}" if query else "")
    request = Request(
        url,
        method="GET",
        headers={"X-WorkPanda-Ops-Token": token, "Accept": "application/json"},
    )
    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        # 只回传状态码，不回显响应体（可能含内部信息），更不带凭据。
        raise RuntimeError(f"运维接口返回 HTTP {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError(f"无法连接 WorkPanda 运维接口：{type(exc.reason).__name__}") from exc
    except (TimeoutError, OSError) as exc:
        raise RuntimeError(f"运维接口请求失败：{type(exc).__name__}") from exc


def _pick(value: object, fields: tuple[str, ...]) -> object:
    """只保留白名单字段并截断字符串，防止把未预期字段带到控制台。"""

    if isinstance(value, list):
        return [_pick(item, fields) for item in value[:MAX_LIMIT]]
    if isinstance(value, dict):
        out: dict[str, object] = {}
        for key in fields:
            if key not in value:
                continue
            item = value[key]
            if isinstance(item, str):
                out[key] = item[:MAX_TEXT]
            elif isinstance(item, (int, float, bool)) or item is None:
                out[key] = item
            else:
                out[key] = str(type(item).__name__)
        return out
    return value


def _envelope(view: str, data: dict, text: str) -> str:
    payload = {
        "octop_ui": {"renderer": RENDERER, "version": 1},
        "data": {"view": view, "readonly": True, **data},
        "text": text,
    }
    return json.dumps(payload, ensure_ascii=False)


def _failure(view: str, exc: Exception) -> str:
    # 断言这里永远不含 token：_get 的异常消息只带状态码/异常类型。
    return _envelope(
        view,
        {"error": str(exc)[:MAX_TEXT]},
        f"读取失败（只读查询，未产生任何变更）：{exc}",
    )


def _limit(value: object) -> int:
    try:
        return max(1, min(MAX_LIMIT, int(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 20


def _clean_token(value: object) -> str:
    """状态过滤值：只允许字母数字与 . _ -，避免拼进查询串的意外字符。"""

    text = str(value or "")[:64]
    return "".join(ch for ch in text if ch.isalnum() or ch in "._-")


async def workpanda_ops_overview() -> str:
    """查看执行留痕聚合指标与工具线程状态（只读）。"""

    try:
        summary, runtime = await asyncio.gather(
            asyncio.to_thread(_get, "/api/ops/traces/summary", {}),
            asyncio.to_thread(_get, "/api/ops/tool-runtime", {}),
        )
    except Exception as exc:
        return _failure("overview", exc)
    return _envelope(
        "overview",
        {
            "metrics": _pick(summary, SUMMARY_FIELDS),
            "tool_runtime": _pick(runtime, RUNTIME_FIELDS),
        },
        "WorkPanda 执行留痕聚合指标与工具线程状态（只读）",
    )


async def workpanda_ops_tasks(limit: int = 20, status: str = "") -> str:
    """按创建时间倒序列出任务（只读，不含用户输入内容）。"""

    try:
        tasks = await asyncio.to_thread(
            _get,
            "/api/ops/tasks",
            {"limit": _limit(limit), "status": _clean_token(status)},
        )
    except Exception as exc:
        return _failure("tasks", exc)
    items = _pick(tasks, TASK_FIELDS)
    count = len(items) if isinstance(items, list) else 0
    return _envelope("tasks", {"items": items}, f"任务列表（只读），返回 {count} 条")


async def workpanda_ops_traces(task_id: str = "", limit: int = 20) -> str:
    """列出执行留痕（只读，不含模型原文与工具入参）。"""

    try:
        traces = await asyncio.to_thread(
            _get,
            "/api/ops/traces",
            {"task_id": _clean_token(task_id), "limit": _limit(limit)},
        )
    except Exception as exc:
        return _failure("traces", exc)
    items = _pick(traces, TRACE_FIELDS)
    count = len(items) if isinstance(items, list) else 0
    return _envelope("traces", {"items": items}, f"执行留痕（只读），返回 {count} 条")


async def workpanda_ops_audit(limit: int = 20) -> str:
    """列出审计归档条目（只读）。"""

    try:
        audit = await asyncio.to_thread(_get, "/api/ops/audit", {"limit": _limit(limit)})
    except Exception as exc:
        return _failure("audit", exc)
    items = _pick(audit, AUDIT_FIELDS)
    count = len(items) if isinstance(items, list) else 0
    return _envelope("audit", {"items": items}, f"审计归档（只读），返回 {count} 条")


def _register(ctx, name: str, fn, description: str) -> None:
    """注册工具；旧版 SDK 不支持 config_fields 时退回最小签名。

    凭据仍可通过环境变量下发，降级只是少了控制台表单，不影响只读语义。
    """

    try:
        ctx.tool(name, fn, description=description, config_fields=CONFIG_FIELDS)
    except TypeError:
        ctx.tool(name, fn, description=description)


def setup(ctx) -> None:
    """Octop 插件入口：只注册读工具，不注册任何写操作或 Hook。"""

    _register(ctx, "workpanda_ops_overview", workpanda_ops_overview,
              "查看 WorkPanda 执行留痕聚合指标与工具线程状态（只读）")
    _register(ctx, "workpanda_ops_tasks", workpanda_ops_tasks,
              "列出 WorkPanda 任务及状态（只读，不含用户输入内容）")
    _register(ctx, "workpanda_ops_traces", workpanda_ops_traces,
              "列出 WorkPanda 执行留痕（只读，可按 task_id 过滤）")
    _register(ctx, "workpanda_ops_audit", workpanda_ops_audit,
              "列出 WorkPanda 审计归档条目（只读）")
