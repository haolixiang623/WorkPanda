"""每任务一个浏览器会话。

**为什么要有这一层**：Playwright 的同步 API 绑定创建它的线程，而 harness 的
工具调用是"每次调用起一个线程"（见 `tools.RunningToolThreads`）。直接把
`page` 挂在工具实例上，第二次调用就会在另一个线程里碰它——轻则抛
"Sync API inside the asyncio loop"，重则 `greenlet` 报错到看不出原因。

所以每个任务配一个单线程执行器：浏览器、context、page 都在那个线程里创建、
也只在那里使用。工具从任意线程提交操作，由这一层负责排队与回传结果。

**隔离**：`task_id` 是唯一键，一任务一 browser + 一 context，cookie、
localStorage、缓存都不共享。任务之间没有任何复用路径——复用省下的那点启动
时间，换不来"两个群众的登录态串了"这种事故。

**依赖形态**：Playwright 是可选依赖，`playwright` 包本身在 `.[browser]` 里，
浏览器二进制由 `playwright install chromium` 单独装（不进仓库）。所以这里的
导入全部延迟到真正要用的时候，没装也能起服务、跑其余测试。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from workpanda.harness.browser.artifacts import BrowserArtifacts
from workpanda.harness.browser.policy import BrowserPolicy
from workpanda.harness.tools import ToolError

if TYPE_CHECKING:  # 只为类型检查导入，运行时不加载
    from playwright.sync_api import Page, Playwright

T = TypeVar("T")

# 落盘时最多记多少个"填过的值"用于回抹。真实任务填的字段是个位数，
# 设上限只是不让一条坏掉的循环把内存吃掉。
MAX_REMEMBERED_VALUES = 32

# 截图前注入的遮罩：把输入框内容渲染成圆点，**不改值**。改值会破坏页面状态，
# 而这一层只该负责"别把看到的写进文件"。
_MASK_INPUTS_JS = """
() => {
  if (document.getElementById('wp-occlude')) { return true; }
  const style = document.createElement('style');
  style.id = 'wp-occlude';
  style.textContent =
    'input:not([type=hidden]), textarea { -webkit-text-security: disc !important; }';
  document.head.appendChild(style);
  return true;
}
"""

_UNMASK_INPUTS_JS = """
() => {
  const node = document.getElementById('wp-occlude');
  if (node) { node.remove(); }
  return true;
}
"""

_SYNC_API: Any | None = None


def _playwright_module() -> Any:
    """取到 `playwright.sync_api`；没装就翻译成一句人能看懂的话。"""

    global _SYNC_API
    if _SYNC_API is None:
        try:
            from playwright import sync_api
        except ImportError as exc:  # pragma: no cover - 取决于部署是否装可选依赖
            raise ToolError(
                "browser_unavailable",
                "服务端没有安装浏览器执行组件"
                "（pip install '.[browser]' 并 playwright install chromium）。",
            ) from exc
        _SYNC_API = sync_api
    return _SYNC_API


def is_timeout_error(exc: BaseException) -> bool:
    """Playwright 的超时异常。用于把"等待没等到"与"浏览器坏了"分开。

    走函数而不是直接 `isinstance`，是因为模块顶层不能 import playwright——
    没装可选依赖时整个服务都不该起不来。
    """

    try:
        sync_api = _playwright_module()
    except ToolError:  # pragma: no cover - 走到这里说明调用链已经不对了
        return False
    return isinstance(exc, sync_api.TimeoutError)


class BrowserSession:
    """一个任务的浏览器：一次启动、多次操作、最后一个线程收尾。"""

    def __init__(
        self,
        policy: BrowserPolicy,
        *,
        headless: bool | None = None,
        task_key: str = "unknown",
    ) -> None:
        self._policy = policy
        self._headless = policy.headless if headless is None else headless
        self._task_key = task_key
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wp-browser")
        self._playwright: Playwright | None = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Page | None = None
        self._closed = False
        self._artifacts: BrowserArtifacts | None = None
        if policy.artifact_dir is not None:
            self._artifacts = BrowserArtifacts(root=Path(policy.artifact_dir))
        self._remembered: list[str] = []
        self.last_used = time.monotonic()

    # ---- 对外 ----

    def run(self, operation: Callable[[Page], T], *, label: str = "") -> T:
        """在线程内执行一次操作，返回结果；异常原样抛回调用线程。

        **刻意不设内部超时**：外层 `ToolSpec.timeout_seconds` 才是唯一时限。
        自己再设一个更短的超时，会把"这次点击到底发出去没有"变成两个人的
        判断；现在只有 harness 一个地方在说"超时了，结果未知"。
        """

        if self._closed:
            raise ToolError("browser_session_closed", "浏览器会话已关闭，请重新打开页面。")

        self.last_used = time.monotonic()
        return self._executor.submit(self._in_actor, operation, label).result()

    def remember_value(self, value: str) -> None:
        """记下这次填进页面的值，落盘前从 DOM 里回抹掉。"""

        text = str(value)
        if len(text) < 4 or text in self._remembered:
            return
        self._remembered.append(text)
        if len(self._remembered) > MAX_REMEMBERED_VALUES:
            del self._remembered[0]

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._executor.submit(self._teardown).result(timeout=20)
        except Exception:  # noqa: BLE001 - 收尾失败不能盖住调用方原来的错误
            pass
        finally:
            self._executor.shutdown(wait=False, cancel_futures=True)

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def idle_seconds(self) -> float:
        return time.monotonic() - self.last_used

    # ---- 线程内 ----

    def _in_actor(self, operation: Callable[[Page], T], label: str) -> T:
        sync_api = _playwright_module()
        page = self._ensure_page(sync_api)
        try:
            result = operation(page)
        except ToolError as exc:
            raise ToolError(exc.code, exc.message + self._dump_failure(label)) from exc
        except sync_api.TimeoutError as exc:
            # 元素没出现、页面没响应：这是**明确的失败**，不是"结果未知"。
            # 上层据此转人工，不许重试同一个动作。
            raise ToolError(
                "element_timeout",
                f"页面在限期内没有出现预期元素：{exc}" + self._dump_failure(label),
            ) from exc
        except sync_api.Error as exc:
            raise ToolError(
                "browser_error",
                f"浏览器操作失败：{exc}" + self._dump_failure(label),
            ) from exc
        # 每次操作后复查落地站点：跨 origin 交接就发生在一次点击里，
        # 只校验 open 的入参等于没校验。
        self._check_landing()
        return result

    def _ensure_page(self, sync_api: Any) -> Page:
        if self._page is not None:
            return self._page

        self._playwright = sync_api.sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=self._headless)
        self._context = self._browser.new_context(
            # 本阶段不下载：连下载能力都不给，比"给一个限定目录"更没有余地。
            # 点击下载类元素在策略层先被拒（`download_blocked`），这里是第二道。
            accept_downloads=False,
            viewport={"width": 1280, "height": 900},
        )
        self._page = self._context.new_page()
        self._page.set_default_navigation_timeout(self._policy.navigation_timeout_ms)
        self._page.set_default_timeout(self._policy.element_timeout_ms)
        return self._page

    def _check_landing(self) -> None:
        page = self._page
        if page is None:
            return
        url = page.url
        if not url or url.startswith("about:"):
            return
        try:
            self._policy.check_landing(url)
        except ToolError as exc:
            # 跳出白名单：先留一份现场（这正是最需要人看的一刻），再销毁会话，
            # 不留一个还在外面跑着的页面。会话标记为已关闭，下次调用会重建。
            note = self._dump_failure("landing")
            self._closed = True
            self._teardown()
            raise ToolError(exc.code, exc.message + note) from exc

    # ---- 现场留档 ----

    def _dump_failure(self, label: str) -> str:
        """把失败现场写进磁盘，返回可直接拼进错误信息的说明。

        留档是**尽力而为**：写不进去不能改变原来的失败码——那会让"页面改版"看起来
        像"磁盘满了"。
        """

        if self._artifacts is None or self._page is None:
            return ""
        page = self._page
        screenshot: bytes | None = None
        dom = ""
        try:
            try:
                page.evaluate(_MASK_INPUTS_JS)
                screenshot = page.screenshot(type="png", full_page=False)
            except Exception:  # noqa: BLE001 - 截图失败不影响 DOM 留档
                screenshot = None
            finally:
                try:
                    page.evaluate(_UNMASK_INPUTS_JS)
                except Exception:  # noqa: BLE001 - 页面可能已经没了
                    pass
            dom = page.content()
        except Exception:  # noqa: BLE001 - 页面已崩、导航中被打断都会走到这里
            dom = ""

        if not dom and screenshot is None:
            return ""
        paths = self._artifacts.write(
            task_id=self._task_key,
            label=label,
            dom_html=dom,
            screenshot=screenshot,
            secrets=list(self._remembered),
        )
        if not paths:
            return "（现场留档失败：目录不可写）"
        return "\n现场已留档（已脱敏）：" + "、".join(str(path) for path in paths)

    def _teardown(self) -> None:
        for closer in (
            lambda: self._context.close() if self._context else None,
            lambda: self._browser.close() if self._browser else None,
            lambda: self._playwright.stop() if self._playwright else None,
        ):
            try:
                closer()
            except Exception:  # noqa: BLE001 - 收尾尽力而为
                pass
        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None


class BrowserSessions:
    """进程级会话表：`task_id → BrowserSession`，带空闲回收。"""

    def __init__(self, policy: BrowserPolicy, *, headless: bool | None = None) -> None:
        self._policy = policy
        self._headless = headless
        self._lock = threading.Lock()
        self._sessions: dict[str, BrowserSession] = {}

    def for_task(self, task_id: Any) -> BrowserSession:
        key = str(task_id)
        self._reap()
        with self._lock:
            session = self._sessions.get(key)
            if session is None or session.closed:
                session = BrowserSession(
                    self._policy, headless=self._headless, task_key=key
                )
                self._sessions[key] = session
            return session

    def close_task(self, task_id: Any) -> None:
        with self._lock:
            session = self._sessions.pop(str(task_id), None)
        if session is not None:
            session.close()

    def close_all(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.close()

    def stats(self) -> dict[str, int]:
        with self._lock:
            live = sum(1 for session in self._sessions.values() if not session.closed)
        return {"sessions": live}

    def _reap(self) -> None:
        """回收空闲会话。任务结束没有回调，只能靠"多久没人用过"判断。

        先摘名单、再关会话：关一个会话要等它的线程收尾（最多 20 秒），
        抱着锁等就等于把所有人的工具调用一起堵在这里。
        """

        ttl = self._policy.session_ttl_seconds
        if ttl <= 0:
            return
        with self._lock:
            expired = [
                key
                for key, session in self._sessions.items()
                if session.closed or session.idle_seconds > ttl
            ]
        for key in expired:
            self.close_task(key)
