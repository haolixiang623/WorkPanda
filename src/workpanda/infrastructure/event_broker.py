"""进程内事件总线。

用于把任务事件即时推送给在线订阅者。它只做通知，不承担持久化：
断线重连仍以 Task Store 中的事件流为准，避免"推送丢了就丢了"。

线程模型：任务执行放在工作线程里（见 `api/routes.py` 的 `asyncio.to_thread`），
因此 `publish` 可能从**非事件循环线程**调用。`asyncio.Queue.put_nowait` 不是
线程安全的，这里按订阅时记下的事件循环把投递排回它自己的线程；推送丢了也不会
丢事件——SSE 断线重连会从 Task Store 补齐。
"""

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from threading import Lock
from uuid import UUID


class EventBroker:
    def __init__(self) -> None:
        # 队列 -> 订阅者所在的事件循环。记住循环才能安全地跨线程投递。
        self._subscribers: dict[UUID, dict[asyncio.Queue[int], asyncio.AbstractEventLoop]] = (
            defaultdict(dict)
        )
        self._lock = Lock()

    def publish(self, task_id: UUID, sequence: int) -> None:
        try:
            caller_loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            # 同步代码或工作线程：没有"当前循环"，全部走线程安全投递。
            caller_loop = None

        with self._lock:
            targets = tuple(self._subscribers.get(task_id, {}).items())

        for queue, loop in targets:
            if loop is caller_loop:
                # 同一个循环里直接放，保序且没有调度延迟。
                queue.put_nowait(sequence)
                continue
            try:
                loop.call_soon_threadsafe(queue.put_nowait, sequence)
            except RuntimeError:
                # 订阅者所在循环已关闭：通知丢了，但 Task Store 仍是权威事件流。
                pass

    @asynccontextmanager
    async def subscribe(self, task_id: UUID) -> AsyncIterator[asyncio.Queue[int]]:
        queue: asyncio.Queue[int] = asyncio.Queue()
        loop = asyncio.get_running_loop()
        with self._lock:
            self._subscribers[task_id][queue] = loop
        try:
            yield queue
        finally:
            with self._lock:
                subscribers = self._subscribers.get(task_id)
                if subscribers is not None:
                    subscribers.pop(queue, None)
                    if not subscribers:
                        self._subscribers.pop(task_id, None)

    def subscriber_count(self, task_id: UUID) -> int:
        with self._lock:
            return len(self._subscribers.get(task_id, ()))

    def total_subscriber_count(self) -> int:
        """所有任务上的活跃订阅总数，即当前进程持有的 SSE 连接数。

        只统计本进程：`EventBroker` 是进程内的，多实例部署时每个实例各报各的，
        调用方需要按实例求和。`docs/CAPACITY-RUNBOOK.md` 说明了这点——单看一个
        实例的数字去判断整体容量会低估。取总数与取单任务数用同一把锁，保证快照
        内部一致，不会把「刚加入的连接」算进总数却没算进某个任务。
        """

        with self._lock:
            return sum(len(subscribers) for subscribers in self._subscribers.values())
