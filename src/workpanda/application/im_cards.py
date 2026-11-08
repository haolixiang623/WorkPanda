"""出站卡片的后台投递（Issue #64）。

为什么不是"在任务推进到某一步时同步调一次发送接口"：

- IM 平台的可用性与任务推进的正确性必须解耦。同步发送意味着对方接口慢 3 秒，
  每一步就慢 3 秒；对方挂了，任务推进就跟着挂。**通知是旁路，不是流程的一部分**；
- 队列满时丢卡片而不是阻塞流程。丢掉的卡片有日志，阻塞的流程没有任何补救。

代价是"卡片可能比状态晚几秒到"。这是可以接受的：卡片本来就只是"去看一眼"的
提示，权威状态永远在任务库里。

`NotifyingEventPublisher` 是事件总线外面的一层扇出：先 `publish` 给在线订阅者
（SSE 的延迟不该被 IM 影响），再往队列里投一个任务号。它从不抛错——即使在
`publish` 之外出问题，也只影响通知，不影响事件已经落库这件事。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from queue import Full, Queue
from threading import Condition, Lock, Thread
from uuid import UUID

logger = logging.getLogger(__name__)


class CardQueue:
    """一个有界队列 + 单个后台线程。

    单线程而不是线程池：发卡是低频动作（每个"值得发"的状态一次），并发发送
    换来的吞吐在试点规模下毫无意义，却会带来顺序与并发数的未知。
    """

    def __init__(
        self,
        handler: Callable[[UUID], None],
        *,
        capacity: int = 200,
        name: str = "im-cards",
    ) -> None:
        self._handler = handler
        self._queue: Queue[object] = Queue(maxsize=max(1, capacity))
        self._stop = object()
        self._lock = Lock()
        self._idle = Condition(self._lock)
        self._pending = 0
        self._thread = Thread(target=self._run, name=name, daemon=True)
        self._started = False

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._thread.start()
            self._started = True

    @property
    def pending(self) -> int:
        with self._lock:
            return self._pending

    def enqueue(self, task_id: UUID) -> bool:
        """投递一个任务号。队列满时返回 False（并记日志），**绝不阻塞调用方**。"""

        self.start()
        with self._lock:
            self._pending += 1
        try:
            self._queue.put_nowait(task_id)
        except Full:
            with self._lock:
                self._pending -= 1
                self._idle.notify_all()
            # 丢卡片的代价是"这一次没收到提醒"，阻塞的代价是"流程卡住"。
            logger.warning("im card queue is full, dropped notification for task %s", task_id)
            return False
        return True

    def flush(self, timeout: float = 10.0) -> bool:
        """等队列排空并在途处理完成。测试与优雅停机用；返回是否真的排空。"""

        with self._idle:
            return self._idle.wait_for(lambda: self._pending == 0, timeout=timeout)

    def close(self, timeout: float = 5.0) -> None:
        if not self._started:
            return
        try:
            self._queue.put(self._stop, timeout=timeout)
        except Full:  # pragma: no cover - 只有队列被塞满且线程卡住才会走到
            logger.warning("im card queue still full on close; worker may be dropped")
            return
        self._thread.join(timeout=timeout)

    # ---------- 内部 ----------

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is self._stop:
                    return
                assert isinstance(item, UUID)
                self._handler(item)
            except Exception:  # noqa: BLE001 - 一张卡发不出去不能把线程带走
                logger.warning("im card delivery failed", exc_info=True)
            finally:
                self._queue.task_done()
                if item is not self._stop:
                    with self._idle:
                        self._pending -= 1
                        self._idle.notify_all()


class NotifyingEventPublisher:
    """事件发布扇出：先给在线订阅者，再给 IM 通知队列。

    顺序固定为"先 SSE、后 IM"，因为两者对延迟的敏感度差一个量级；而且这样即使
    IM 侧出问题，群众侧的实时事件流也已经在推送了。
    """

    def __init__(self, publisher, cards: CardQueue | None = None) -> None:
        self._publisher = publisher
        self._cards = cards

    def publish(self, task_id: UUID, sequence: int) -> None:
        self._publisher.publish(task_id, sequence)
        if self._cards is None:
            return
        try:
            self._cards.enqueue(task_id)
        except Exception:  # noqa: BLE001 - 通知永远不能反噬事件发布
            logger.warning("failed to enqueue im notification for task %s", task_id)
