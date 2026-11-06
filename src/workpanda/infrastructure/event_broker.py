"""进程内事件总线。

用于把任务事件即时推送给在线订阅者。它只做通知，不承担持久化：
断线重连仍以 Task Store 中的事件流为准，避免"推送丢了就丢了"。
"""

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID


class EventBroker:
    def __init__(self) -> None:
        self._subscribers: dict[UUID, set[asyncio.Queue[int]]] = defaultdict(set)

    def publish(self, task_id: UUID, sequence: int) -> None:
        for queue in tuple(self._subscribers.get(task_id, ())):
            queue.put_nowait(sequence)

    @asynccontextmanager
    async def subscribe(self, task_id: UUID) -> AsyncIterator[asyncio.Queue[int]]:
        queue: asyncio.Queue[int] = asyncio.Queue()
        self._subscribers[task_id].add(queue)
        try:
            yield queue
        finally:
            self._subscribers[task_id].discard(queue)
            if not self._subscribers[task_id]:
                self._subscribers.pop(task_id, None)

    def subscriber_count(self, task_id: UUID) -> int:
        return len(self._subscribers.get(task_id, ()))
