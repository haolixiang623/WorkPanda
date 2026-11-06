"""API 依赖和应用级服务容器。"""

from functools import lru_cache

from workpanda.application.task_service import TaskService
from workpanda.infrastructure.memory_store import InMemoryTaskStore


@lru_cache
def get_task_service() -> TaskService:
    return TaskService(InMemoryTaskStore())
