"""应用装配：把配置、存储、适配器和应用服务组合成可运行容器。

测试通过替换这里的依赖来构造不同的存储或执行器，而不修改业务代码。
"""

from dataclasses import dataclass, field

from workpanda.application.ports import Executor, IntentRouter
from workpanda.application.runner import FlowRunner
from workpanda.application.task_service import TaskService
from workpanda.config import Settings, get_settings
from workpanda.domain.flow import FlowDefinition, FlowRegistry
from workpanda.domain.repository import ConfirmationStore, TaskStore
from workpanda.infrastructure.adapters import KeywordIntentRouter, ScriptedExecutor
from workpanda.infrastructure.demo_flows import demo_flows
from workpanda.infrastructure.event_broker import EventBroker
from workpanda.infrastructure.memory_store import InMemoryConfirmationStore, InMemoryTaskStore
from workpanda.infrastructure.sqlite_store import SqliteStore


@dataclass
class Container:
    settings: Settings
    store: TaskStore
    confirmations: ConfirmationStore
    broker: EventBroker
    executor: Executor
    router: IntentRouter
    flows: FlowRegistry
    tasks: TaskService
    closers: list = field(default_factory=list)

    def close(self) -> None:
        for closer in self.closers:
            closer()


def build_container(
    settings: Settings | None = None,
    *,
    flows: list[FlowDefinition] | None = None,
    executor: Executor | None = None,
    router: IntentRouter | None = None,
) -> Container:
    settings = settings or get_settings()

    store: TaskStore
    confirmations: ConfirmationStore
    closers: list = []

    if settings.store_backend == "sqlite":
        sqlite_store = SqliteStore(settings.sqlite_path)
        store = sqlite_store
        confirmations = sqlite_store
        closers.append(sqlite_store.close)
    else:
        store = InMemoryTaskStore()
        confirmations = InMemoryConfirmationStore()

    registered = flows if flows is not None else (demo_flows() if settings.enable_demo_flow else [])
    registry = FlowRegistry(registered)
    broker = EventBroker()
    executor_instance = executor or ScriptedExecutor()
    router_instance = router or KeywordIntentRouter()

    runner = FlowRunner(
        store=store,
        confirmations=confirmations,
        executor=executor_instance,
        publisher=broker,
        flows=registry,
        confirmation_ttl_seconds=settings.confirmation_ttl_seconds,
    )

    tasks = TaskService(
        store=store,
        confirmations=confirmations,
        runner=runner,
        flows=registry,
        router=router_instance,
        confidence_threshold=settings.intent_confidence_threshold,
    )

    return Container(
        settings=settings,
        store=store,
        confirmations=confirmations,
        broker=broker,
        executor=executor_instance,
        router=router_instance,
        flows=registry,
        tasks=tasks,
        closers=closers,
    )
