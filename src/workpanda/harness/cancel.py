"""取消：把"我不想再跑了"变成执行循环能看见的信号。

为什么需要它：没有取消时，唯一的停法是等预算耗尽——用户已经在界面上点了取消，
执行侧仍会把剩下的模型轮次和工具调用跑完，甚至把一笔本可以不发生的提交发出去。
"任务状态改成 cancelled"只是账面上的，正在跑的执行并不知道。

四条边界（与超时、重试不同，取消是**人的决定**，不是故障）：

1. **只在边界生效**：模型调用与工具调用都是同步阻塞的，线程无法被强制打断。
   取消在"下一轮模型调用之前、下一条工具调用之前、重试退避等待中"生效；
   已经提交的那一次调用会跑完并照常留痕——假装它没发生才是真正的危险。
2. **取消也要留痕**：停下来时记 `StopReason.CANCELLED`，结论是"未确认"而不是
   "失败"，因为此前可能已经产生了副作用。谁来核对、核对什么，交给人工。
3. **不丢已发生的事**：取消不清理断点，也不擦除调用记录。用户取消的是"继续跑"，
   不是"刚才那笔没发生"。
4. **可先取消后执行**：取消请求可能比执行更早到达（排队中、别的线程里）。
   登记表会把它暂存起来，等这一步真正开始时立刻生效，而不是被无声丢弃。

线程模型：`CancelToken` 与 `CancelRegistry` 都允许跨线程调用。取消方通常来自
HTTP 请求线程，被取消方是执行线程，两者只通过锁与 `threading.Event` 交互。

跨进程：进程内登记表只在自己的进程里看得见。多实例部署时可以给它接一个共享的
`CancelSignalStore`，取消时先写共享信号、执行侧在边界上主动拉取。写共享信号失败
一律报错（不能只翻本地状态假装取消成功），读不到信号一律按"已取消"停下——
状态不明时继续产生新动作比误停危险得多。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from threading import Event, Lock
from time import monotonic
from typing import Protocol
from uuid import UUID

# 暂存的取消请求保留多久。取消的是"这个任务"而不是"这一次执行"，
# 因此任务真正开始前到达的取消必须仍然有效；但也不能无限期留存，
# 否则一个早就结束的任务会永远被一条陈旧的取消拦住。
DEFAULT_PENDING_TTL_SECONDS = 600.0
DEFAULT_MAX_PENDING = 1000
# 退避等待里轮询共享信号的粒度。太长会让跨进程取消显得"点了没反应"。
DEFAULT_POLL_SECONDS = 0.1
# 共享信号读不出来时的取消原因：写入 trace 后能一眼看出这是"状态不明"而非用户本意。
SIGNAL_UNAVAILABLE_REASON = "cancel_signal_unavailable"


class CancelSignalError(RuntimeError):
    """共享取消信号没写成功：此时不能声称取消已生效。"""


class CancelSignalStore(Protocol):
    """跨进程可见的取消信号。实现必须自己保证并发写与过期清理。"""

    def signal(self, task_id: UUID, reason: str, *, ttl_seconds: float) -> None:
        """记录一次取消。写失败必须抛异常，让调用方如实报告"没送达"。"""
        ...

    def reason_for(self, task_id: UUID) -> str | None:
        """返回仍有效的取消原因；没有或已过期返回 None。"""
        ...


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _clean_reason(reason: object) -> str:
    text = str(reason or "").strip()
    return text[:64] if text else "cancelled"


class Cancelled(Exception):
    """执行循环内部用来中止的信号。

    只在 harness 内部抛出与捕获，不会漏到应用层：执行器把它翻译成
    `unconfirmed` + `cancelled`，与超时、预算耗尽走同一条收尾路径。
    """

    def __init__(self, reason: str, stage: str) -> None:
        super().__init__(f"cancelled at {stage}: {reason}")
        self.reason = _clean_reason(reason)
        # 在哪个边界上停下的：before_model / before_tool / retry_backoff。
        # 事后排查"取消之后为什么还发出去一笔"时，这个字段是第一手信息。
        self.stage = stage


class CancelToken:
    """一次执行对应一个令牌。第一个取消原因生效，后续取消不覆盖。

    `poll` 是可选的跨进程信号拉取函数：没有它时行为与进程内版本完全一致；
    有它时，每次边界检查与退避等待都会主动拉一次共享状态，不依赖另一个进程
    能碰到本进程内存里的 `Event`。
    """

    def __init__(
        self,
        *,
        task_id: UUID | None = None,
        step_id: str | None = None,
        poll: Callable[[], str | None] | None = None,
        poll_interval: float = DEFAULT_POLL_SECONDS,
    ) -> None:
        self.task_id = task_id
        self.step_id = step_id
        self._event = Event()
        self._lock = Lock()
        self._reason: str | None = None
        self._cancelled_at: datetime | None = None
        self._poll = poll
        # 下限避免"轮询间隔 0"把退避变成忙等；上限避免取消要等一整秒才有反应。
        self._poll_interval = max(0.01, min(poll_interval, 1.0))

    def cancel(self, reason: str = "cancelled") -> bool:
        """请求取消。返回是否是本次真正把状态翻过去的（重复取消返回 False）。"""

        with self._lock:
            if self._reason is not None:
                return False
            self._reason = _clean_reason(reason)
            self._cancelled_at = _utc_now()
            self._event.set()
            return True

    def _refresh(self) -> None:
        """在边界上拉一次共享信号。读不出来按"已取消"处理，绝不带着疑问继续。"""

        if self._poll is None or self._event.is_set():
            return
        try:
            reason = self._poll()
        except Exception:  # noqa: BLE001 - 状态不明时停下比继续调用工具安全
            self.cancel(SIGNAL_UNAVAILABLE_REASON)
            return
        if reason:
            self.cancel(_clean_reason(reason))

    @property
    def cancelled(self) -> bool:
        self._refresh()
        return self._event.is_set()

    @property
    def reason(self) -> str | None:
        return self._reason

    @property
    def cancelled_at(self) -> datetime | None:
        return self._cancelled_at

    def raise_if_cancelled(self, stage: str) -> None:
        """在边界上检查。已取消就抛出 `Cancelled`，由执行循环收尾。"""

        self._refresh()
        if self._event.is_set():
            raise Cancelled(self._reason or "cancelled", stage)

    def wait(self, seconds: float) -> bool:
        """可被取消打断的等待，用于重试退避。

        返回 True 表示"等到一半被取消了"。用 `Event.wait` 而不是 `sleep`：
        否则取消后仍要把退避时间耗完，用户看到的就是"点了没反应"。
        接了共享信号时按 `_poll_interval` 切片等待，每片结束拉一次共享状态。
        """

        self._refresh()
        if self._event.is_set():
            return True
        if seconds <= 0:
            return False
        if self._poll is None:
            return self._event.wait(seconds)

        deadline = monotonic() + seconds
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                return False
            if self._event.wait(min(self._poll_interval, remaining)):
                return True
            self._refresh()
            if self._event.is_set():
                return True

    def snapshot(self) -> dict[str, object]:
        return {
            "task_id": str(self.task_id) if self.task_id else None,
            "step_id": self.step_id,
            "cancelled": self.cancelled,
            "reason": self._reason,
            "cancelled_at": self._cancelled_at.isoformat() if self._cancelled_at else None,
        }


class CancelRegistry:
    """任务到令牌的登记表，并提供"取消这个任务"的唯一入口。

    两种情形都要照顾到：

    - 执行正在进行：找到该任务名下所有活令牌，逐个置位；
    - 执行还没开始（排队、或在别的线程里等锁）：暂存取消原因，
      这一步开始取令牌时立刻生效。
    """

    def __init__(
        self,
        *,
        pending_ttl_seconds: float = DEFAULT_PENDING_TTL_SECONDS,
        max_pending: int = DEFAULT_MAX_PENDING,
        shared: CancelSignalStore | None = None,
        poll_interval: float = DEFAULT_POLL_SECONDS,
        signal_ttl_seconds: float = DEFAULT_PENDING_TTL_SECONDS,
    ) -> None:
        self._pending_ttl_seconds = max(1.0, pending_ttl_seconds)
        self._max_pending = max(1, max_pending)
        self._shared = shared
        self._poll_interval = poll_interval
        self._signal_ttl_seconds = max(1.0, signal_ttl_seconds)
        self._tokens: dict[tuple[UUID, str], CancelToken] = {}
        self._pending: dict[UUID, tuple[str, float]] = {}
        self._lock = Lock()

    # ---------- 执行侧 ----------

    def begin(self, task_id: UUID, step_id: str) -> CancelToken:
        """取（或新建）本步骤的令牌，并消费此前暂存的取消请求。"""

        key = (task_id, step_id)
        with self._lock:
            token = self._tokens.get(key)
            if token is None:
                token = CancelToken(
                    task_id=task_id,
                    step_id=step_id,
                    poll=self._poll_for(task_id),
                    poll_interval=self._poll_interval,
                )
                self._tokens[key] = token
            pending = self._take_pending(task_id)
        if pending is not None:
            token.cancel(pending)
        return token

    def end(self, task_id: UUID, step_id: str) -> None:
        """执行结束（无论结论如何）后释放令牌，避免登记表随任务数量增长。"""

        with self._lock:
            self._tokens.pop((task_id, step_id), None)

    # ---------- 取消侧 ----------

    def cancel_task(self, task_id: UUID, reason: str = "user_cancelled") -> int:
        """取消某任务名下正在执行的步骤，返回被置位的令牌数。

        返回 0 不代表"什么都没发生"：可能被执行还没开始，此时会暂存起来，
        在 `begin` 时生效。
        """

        cleaned = _clean_reason(reason)
        if self._shared is not None:
            # 先写共享信号：写不成功时另一个进程里正在跑的执行不会知道这件事，
            # 因此必须报错，不能只翻本进程的状态就返回成功。
            try:
                self._shared.signal(task_id, cleaned, ttl_seconds=self._signal_ttl_seconds)
            except Exception as exc:  # noqa: BLE001 - 底层异常类型不属于本层协议
                raise CancelSignalError("cancel signal could not be delivered") from exc
        with self._lock:
            tokens = [token for (owner, _), token in self._tokens.items() if owner == task_id]
            if not tokens:
                self._remember_pending(task_id, cleaned)
                return 0
        return sum(1 for token in tokens if token.cancel(cleaned))

    def pending_reason(self, task_id: UUID) -> str | None:
        """查询是否有暂存的取消请求（只读，不消费）。"""

        with self._lock:
            return self._take_pending(task_id, consume=False)

    def __len__(self) -> int:
        with self._lock:
            return len(self._tokens)

    # ---------- 内部 ----------

    def _poll_for(self, task_id: UUID) -> Callable[[], str | None] | None:
        """给令牌绑一个只查本任务的共享信号拉取函数。

        不在这里主动读一次：读操作统一由令牌在边界上触发，失败也由令牌按
        "状态不明 = 已取消"收口，装配层不必关心执行循环何时检查。
        """

        if self._shared is None:
            return None
        shared = self._shared

        def poll() -> str | None:
            return shared.reason_for(task_id)

        return poll

    def _take_pending(self, task_id: UUID, *, consume: bool = True) -> str | None:
        entry = self._pending.get(task_id)
        if entry is None:
            return None
        reason, at = entry
        if monotonic() - at > self._pending_ttl_seconds:
            # 过期的取消不能拦住后来的一次新执行。
            self._pending.pop(task_id, None)
            return None
        if consume:
            self._pending.pop(task_id, None)
        return reason

    def _remember_pending(self, task_id: UUID, reason: str) -> None:
        if len(self._pending) >= self._max_pending:
            # 登记表有界：塞满时淘汰最早的一条，避免"取消风暴"把内存吃光。
            oldest = min(self._pending, key=lambda key: self._pending[key][1])
            self._pending.pop(oldest, None)
        self._pending[task_id] = (reason, monotonic())

