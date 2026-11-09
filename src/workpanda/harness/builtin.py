"""内置受控工具。

只放"与业务无关、但任何事项都可能用到"的能力。三个样本分别代表三类风险：

- `sys_clock`：纯本地、无副作用，低风险；
- `http_fetch`：触达外部网络，中风险——必须域名白名单、禁私网地址、禁跳转；
- `browser_*`：用真实浏览器操作三方站，中高风险——白名单（含跳转后的站点）、
  一任务一 context、写操作默认全拒。

新增工具时必须先确认：默认不可用、显式授权才可用、失败不产生副作用。
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx

from workpanda.harness.browser.policy import BrowserPolicy
from workpanda.harness.browser.session import BrowserSessions
from workpanda.harness.browser.tools import browser_tools
from workpanda.harness.models import RiskLevel, ToolSpec
from workpanda.harness.policy import PolicyChecklistTool
from workpanda.harness.tools import (
    DEFAULT_MAX_STUCK_THREADS,
    ToolContext,
    ToolError,
    ToolRegistry,
)


class SystemClockTool:
    """返回当前时间。用于验证"evidence 来自真实工具返回值"这条链路。"""

    spec = ToolSpec(
        name="sys_clock",
        description="获取当前时间（UTC 与本地时区），参数为空。",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        risk=RiskLevel.LOW,
        idempotent=True,
        timeout_seconds=2.0,
    )

    def invoke(self, arguments: dict[str, Any], context: ToolContext) -> str:
        now = datetime.now(UTC)
        local = now.astimezone()
        return (
            f"utc={now.isoformat(timespec='seconds')} "
            f"local={local.isoformat(timespec='seconds')} "
            f"unix={int(now.timestamp())}"
        )


class HttpFetchTool:
    """按域名白名单抓取公开页面文本。

    安全约束（缺一不可）：
    - 仅 https；
    - 主机必须在白名单内（精确匹配，或以 `.` 开头的后缀匹配）；
    - 解析出的所有地址都必须是公网地址；
    - 不跟随重定向，避免绕过白名单；
    - 限制响应体大小与超时。
    """

    def __init__(
        self,
        *,
        allowed_hosts: Sequence[str],
        timeout_seconds: float = 8.0,
        max_chars: int = 4000,
        client: httpx.Client | None = None,
    ) -> None:
        self._allowed_hosts = tuple(host.strip().lower() for host in allowed_hosts if host.strip())
        self._max_chars = max_chars
        self._client = client or httpx.Client(timeout=timeout_seconds, follow_redirects=False)
        self._owns_client = client is None
        self.spec = ToolSpec(
            name="http_fetch",
            description="抓取白名单域名下的公开网页文本，仅支持 https。",
            parameters={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "要抓取的 https 地址",
                        "maxLength": 500,
                    }
                },
                "required": ["url"],
                "additionalProperties": False,
            },
            risk=RiskLevel.MEDIUM,
            idempotent=True,
            timeout_seconds=timeout_seconds,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def invoke(self, arguments: dict[str, Any], context: ToolContext) -> str:
        url = str(arguments["url"])
        parsed = urlparse(url)

        if parsed.scheme != "https":
            raise ToolError("scheme_not_allowed", "只允许 https 地址。")
        if parsed.username or parsed.password:
            raise ToolError("credentials_in_url", "地址中不得包含凭据。")
        host = (parsed.hostname or "").lower()
        if not host:
            raise ToolError("invalid_url", "无法解析主机名。")
        if not self._host_allowed(host):
            raise ToolError("host_not_allowed", f"主机 {host} 不在白名单内。")

        _ensure_public_address(host)

        timeout = self.spec.timeout_seconds
        if context.remaining_seconds > 0:
            timeout = min(timeout, context.remaining_seconds)
        if timeout <= 0:
            raise ToolError("no_time_budget", "本次执行已无剩余时间。")

        try:
            response = self._client.get(url, timeout=timeout)
        except httpx.TimeoutException as exc:
            raise ToolError("http_timeout", "抓取超时。", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ToolError("http_error", f"网络错误：{type(exc).__name__}") from exc

        if 300 <= response.status_code < 400:
            raise ToolError("redirect_not_allowed", "目标返回重定向，已拒绝跟随。")
        if response.status_code >= 400:
            raise ToolError("http_status", f"目标返回 {response.status_code}。")

        return f"status={response.status_code}\n{response.text[: self._max_chars]}"

    def _host_allowed(self, host: str) -> bool:
        for entry in self._allowed_hosts:
            if entry.startswith("."):
                if host.endswith(entry) or host == entry[1:]:
                    return True
            elif host == entry:
                return True
        return False


def _ensure_public_address(host: str) -> None:
    """解析并校验地址，防止借助 DNS 指向内网（SSRF）。"""

    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise ToolError("dns_failed", f"无法解析主机 {host}。") from exc

    for info in infos:
        address = str(info[4][0])
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            raise ToolError("dns_failed", f"解析结果不可信：{address}") from None
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise ToolError("private_address_blocked", f"主机 {host} 解析到非公网地址，已拒绝。")


def default_registry(
    *,
    allowed_hosts: Sequence[str] = (),
    http_client: httpx.Client | None = None,
    browser_policy: BrowserPolicy | None = None,
    max_stuck_threads: int = DEFAULT_MAX_STUCK_THREADS,
) -> tuple[ToolRegistry, list[Callable[[], None]]]:
    """构造默认工具注册表。

    返回 `(registry, closers)`，由装配层负责关闭需要释放的资源。
    """

    registry = ToolRegistry(max_stuck_threads=max_stuck_threads)
    registry.register(SystemClockTool())
    # 内置口径查询：纯本地字典、不触网、无副作用，因此不需要白名单也不该被开关关掉。
    # 它"无条件注册"是安全的，但**能不能在某一步调用**仍由步骤授权决定——注册只代表
    # 具备能力，不代表任何事项都能用。
    registry.register(PolicyChecklistTool())

    closers: list[Callable[[], None]] = []
    # 未配置白名单时不注册 http 工具：默认不可用比默认全开更安全。
    if allowed_hosts:
        fetch = HttpFetchTool(allowed_hosts=allowed_hosts, client=http_client)
        registry.register(fetch)
        closers.append(fetch.close)
    # 浏览器工具同理：没给白名单就一个都不注册，模型连工具名都看不到。
    # 注册了也只是"具备能力"，某一步能不能调仍由步骤授权决定。
    if browser_policy is not None and browser_policy.enabled:
        sessions = BrowserSessions(browser_policy)
        for tool in browser_tools(sessions, browser_policy):
            registry.register(tool)
        closers.append(sessions.close_all)
    return registry, closers
