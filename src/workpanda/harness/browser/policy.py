"""浏览器工具的策略层：能去哪、能点什么、什么时候必须拒绝。

这一层是纯函数，不碰 Playwright。好处是每一条红线都能在没有浏览器的环境里
回归——真正会出事的三类判断（域名、副作用、脱敏边界）不该被"跑不起浏览器"
掩盖掉。

三条红线：

1. **域名**：只认白名单里的 host，**跳转之后落的站点也要在名单里**。跨 origin
   交接是政务站点的常态（账号站 → 办理站），只校验入口等于没校验。
   条目可以带端口（`127.0.0.1:8101`）：同一台主机上的两个端口是两个 origin，
   cookie 作用域、会话隔离、跳转白名单全都不一样，按主机粒度放行等于放大了一圈。
2. **副作用**：本阶段只开到「进入填写页」，任何可能把数据交出去的动作都拒绝。
   判断依据是"元素自身的属性"（`data-testid`、`type`、可访问名、是否有 form
   宿主），不是模型说了什么——模型没有权限替我们决定哪个按钮算提交。
   提交类控件（`type=submit|image`、或 form 内未标 type 的 `<button>`）**默认
   全拒**；登录按钮这类必须先走完流程、又确实是提交控件的目标，只能在
   `permitted_submit_testids` 里逐个放行——放行是一个需要人看过的动作，
   不该由这里替运维猜。
3. **失败语义**：找不到元素、页面改版，都是**明确失败**（抛错误码），
   不是"再试一次"。误报成功比失败严重得多。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from workpanda.harness.tools import ToolError

# 只允许这两类 scheme。`file:`、`javascript:`、`data:` 一律不认：它们能绕过
# 域名白名单，让"打开一个页面"变成"打开本地任意文件"。
ALLOWED_SCHEMES = frozenset({"http", "https"})

# scheme 的默认端口。用于把 `http://a/` 与 `http://a:80/` 看成同一个 origin。
DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443}

# 精确命中的禁止目标。这些是模拟站里产生真实副作用的入口，真实站点接入时
# 由运维在这里补齐——**默认拒绝**比"默认放行再补名单"安全。
FORBIDDEN_TESTIDS: frozenset[str] = frozenset(
    {
        # 最终提交：点了就是一笔真实申请
        "submit-application",
        # 附件上传：P3 的边界是"附件不代传"，服务端不接收、不中转群众材料
        "upload-submit",
        # 删除类：删已上传材料同样是写操作
        "attachment-delete",
    }
)

# 可访问名/按钮文本里出现这些词就当作写操作。中文站点几乎没有别的写法，
# 而英文站点靠提交类控件的默认拒绝兜。
FORBIDDEN_NAME_HINTS: tuple[str, ...] = (
    "提交",
    "上传",
    "确认",
    "删除",
    "支付",
    "付款",
    "签署",
    "下载回执",
)


@dataclass(frozen=True)
class BrowserPolicy:
    """一次浏览器会话要遵守的全部约束。"""

    # 形如 `example.com`、`.example.com`（后缀）、`127.0.0.1:8101`（含端口）。
    # 不带端口的条目放行该主机的任意端口；带端口的只放行这一个 origin。
    allowed_hosts: tuple[str, ...] = ()
    # 允许点击的提交类控件（按 `data-testid` 精确匹配）。默认空：**任何提交类
    # 控件都不许点**。登录按钮要进去只能在这里显式登记，登记即代表"人已经
    # 确认过这一步只建立会话、不提交业务数据"。
    permitted_submit_testids: frozenset[str] = frozenset()
    # 单次页面导航的上限（毫秒）。超时按"明确失败"处理，不重试。
    navigation_timeout_ms: int = 15_000
    # 找元素的上限。给它比导航短：找不到就是改版了，早点把话说明白。
    element_timeout_ms: int = 6_000
    # 单页读回给模型的字符上限，避免把整站 DOM 灌进上下文。
    max_read_chars: int = 4_000
    # 会话空闲多久回收。任务结束没有回调可用，只能靠空闲判定。
    session_ttl_seconds: int = 600
    # 有头/无头。生产一律无头；本机排查时可以开有头看画面。
    headless: bool = True
    # 失败现场（DOM / 截图）落盘目录。为空表示**不留档**——默认不留，
    # 因为留档本身就是在磁盘上多写一份群众数据。
    artifact_dir: Path | None = None
    forbidden_testids: frozenset[str] = field(default_factory=lambda: FORBIDDEN_TESTIDS)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "allowed_hosts",
            tuple(host.strip().lower() for host in self.allowed_hosts if host.strip()),
        )
        object.__setattr__(
            self,
            "permitted_submit_testids",
            frozenset(item.strip() for item in self.permitted_submit_testids if item.strip()),
        )

    @property
    def enabled(self) -> bool:
        return bool(self.allowed_hosts)

    # ---- 域名 ----

    def check_url(self, url: str) -> str:
        """校验并返回原样的 URL。不合格直接抛错，不做任何补救。"""

        parsed = urlparse(url.strip())
        if parsed.scheme not in ALLOWED_SCHEMES:
            raise ToolError(
                "scheme_not_allowed",
                f"只允许 http/https，收到 {parsed.scheme or '空'}。",
            )
        if parsed.username or parsed.password:
            raise ToolError("credentials_in_url", "地址里不得带账号密码。")
        host, port = _authority(parsed)
        if not host:
            raise ToolError("invalid_url", f"无法从 {url!r} 解析出主机名。")
        if not self.host_allowed(host, port):
            raise ToolError("host_not_allowed", f"{_label(host, port)} 不在白名单内。")
        return url

    def host_allowed(self, host: str, port: int | None = None) -> bool:
        """精确匹配，或以 `.` 开头的后缀匹配（与 `http_fetch` 同一套口径）。

        条目带端口时要求端口也一致；不带端口时放行该主机任意端口。
        """

        candidate = host.strip().lower()
        if not candidate:
            return False
        for entry in self.allowed_hosts:
            entry_host, entry_port = _split_entry(entry)
            if not _host_matches(candidate, entry_host):
                continue
            if entry_port is None or entry_port == port:
                return True
        return False

    def check_landing(self, url: str) -> str:
        """跳转落地后的复查。凡是导航结束都走这里——只校验入口是不够的。"""

        parsed = urlparse(url)
        host, port = _authority(parsed)
        if not self.host_allowed(host, port):
            raise ToolError(
                "redirect_host_not_allowed",
                f"页面跳到了白名单之外的 {_label(host, port)}，已停止操作。"
                "跨站交接必须把落地站点也显式列进白名单。",
            )
        return url

    # ---- 点击 ----

    def check_click(
        self,
        *,
        testid: str | None,
        tag: str,
        input_type: str | None,
        accessible_name: str,
        submit_like: bool,
        downloads: bool = False,
    ) -> None:
        """点击前的副作用闸门。任何一条命中就拒绝，且**保证没有派发点击**。"""

        name = accessible_name or ""
        if testid and testid in self.forbidden_testids:
            raise ToolError(
                "submit_click_blocked",
                f"本阶段不开放写操作：`{testid}` 会把数据提交出去。",
            )
        for hint in FORBIDDEN_NAME_HINTS:
            if hint in name:
                raise ToolError(
                    "submit_click_blocked",
                    f"目标的可访问名里含「{hint}」，按写操作拒绝；"
                    "确实需要这一步，请转人工，不要换路径绕开。",
                )
        # 下载有两条路：可访问名像下载，或元素自己带 `download` / 指向文件。
        # 只看名字不够——一张图片链接的文本可能是空的。
        if tag.strip().lower() == "a" and (downloads or _looks_like_download(name)):
            raise ToolError("download_blocked", "本阶段不下载文件。")
        if submit_like and (testid or "") not in self.permitted_submit_testids:
            raise ToolError(
                "submit_click_blocked",
                "目标是提交类控件（会向服务端写入数据），本阶段只开到「进入填写页」。"
                + (
                    f"`{testid}` 若确实只是入口（例如登录），需要运维在"
                    " `HARNESS_BROWSER_PERMITTED_CLICK_TESTIDS` 里逐个登记后才可点击。"
                    if testid
                    else "该控件没有 `data-testid`，无法进入放行名单，请转人工。"
                ),
            )

    def clip(self, text: str) -> str:
        if len(text) <= self.max_read_chars:
            return text
        return text[: self.max_read_chars] + f"\n…（已截断，共 {len(text)} 字）"


def _authority(parsed) -> tuple[str, int | None]:
    """从解析结果里取 (host, port)。端口非法时按拒绝处理，不猜。"""

    host = (parsed.hostname or "").strip().lower()
    try:
        port = parsed.port
    except ValueError as exc:
        raise ToolError("invalid_url", f"地址里的端口不合法：{exc}") from exc
    if port is None:
        port = DEFAULT_PORTS.get(parsed.scheme)
    return host, port


def _label(host: str, port: int | None) -> str:
    return host if port is None else f"{host}:{port}"


def _split_entry(entry: str) -> tuple[str, int | None]:
    """拆出白名单条目的 (host, port)。

    IPv6 必须写成 `[::1]:8101`。裸 `::1` 里没有端口可拆——按整串当主机名，
    匹配不上就是拒绝，比猜错安全。
    """

    value = entry.strip().lower()
    if value.startswith("["):
        host, _, rest = value.partition("]")
        host = host[1:]
        if rest.startswith(":") and rest[1:].isdigit():
            return host, int(rest[1:])
        return host, None
    if value.count(":") == 1:
        host, _, digits = value.partition(":")
        if digits.isdigit():
            return host, int(digits)
        return value, None
    return value, None


def _host_matches(candidate: str, entry_host: str) -> bool:
    if entry_host.startswith("."):
        return candidate == entry_host[1:] or candidate.endswith(entry_host)
    return candidate == entry_host


def _looks_like_download(name: str) -> bool:
    return any(word in name for word in ("下载", "导出", "保存到本地"))


def policy_from_hosts(
    hosts: tuple[str, ...],
    *,
    permitted_submit_testids: frozenset[str] = frozenset(),
    navigation_timeout_ms: int = 15_000,
    element_timeout_ms: int = 6_000,
    max_read_chars: int = 4_000,
    session_ttl_seconds: int = 600,
    headless: bool = True,
    artifact_dir: Path | None = None,
) -> BrowserPolicy:
    return BrowserPolicy(
        allowed_hosts=hosts,
        permitted_submit_testids=permitted_submit_testids,
        navigation_timeout_ms=navigation_timeout_ms,
        element_timeout_ms=element_timeout_ms,
        max_read_chars=max_read_chars,
        session_ttl_seconds=session_ttl_seconds,
        headless=headless,
        artifact_dir=artifact_dir,
    )
