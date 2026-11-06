"""事件总线只负责即时通知，顺序与可靠性由 Task Store 保证。"""

import asyncio
import uuid

from workpanda.infrastructure.event_broker import EventBroker


async def test_subscriber_receives_published_sequence() -> None:
    broker = EventBroker()
    task_id = uuid.uuid4()

    async with broker.subscribe(task_id) as queue:
        broker.publish(task_id, 1)
        broker.publish(task_id, 2)
        assert await asyncio.wait_for(queue.get(), timeout=1) == 1
        assert await asyncio.wait_for(queue.get(), timeout=1) == 2


async def test_subscriber_is_removed_after_disconnect() -> None:
    broker = EventBroker()
    task_id = uuid.uuid4()

    async with broker.subscribe(task_id):
        assert broker.subscriber_count(task_id) == 1

    assert broker.subscriber_count(task_id) == 0
