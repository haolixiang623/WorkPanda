"""应用装配：把配置、存储、适配器和应用服务组合成可运行容器。

测试通过替换这里的依赖来构造不同的存储或执行器，而不修改业务代码。
"""

from collections.abc import Callable
from dataclasses import dataclass, field

from workpanda.application.ports import Executor, IntentRouter
from workpanda.application.runner import FlowRunner
from workpanda.application.task_service import TaskService
from workpanda.config import Settings, get_settings
from workpanda.domain.flow import FlowDefinition, FlowRegistry
from workpanda.domain.identity import SessionSigner, new_secret
from workpanda.domain.repository import ConfirmationStore, TaskStore
from workpanda.harness.audit import AuditJournal, JsonlAuditJournal, NullAuditJournal
from workpanda.harness.builtin import default_registry
from workpanda.harness.cancel import CancelRegistry
from workpanda.harness.cancel_executor import CancellableExecutor
from workpanda.harness.checkpoint import CheckpointStore, InMemoryCheckpointStore
from workpanda.harness.executor import HarnessExecutor
from workpanda.harness.lease import (
    InMemoryStepLeaseStore,
    SqliteStepLeaseStore,
    StepLeaseStore,
)
from workpanda.harness.openai_client import OpenAICompatibleClient
from workpanda.harness.redaction import DEFAULT_REDACTOR, NO_REDACTION
from workpanda.harness.trace import InMemoryTraceStore, TraceStore
from workpanda.infrastructure.adapters import KeywordIntentRouter, ScriptedExecutor
from workpanda.infrastructure.demo_flows import demo_flows
from workpanda.infrastructure.event_broker import EventBroker
from workpanda.infrastructure.memory_store import InMemoryConfirmationStore, InMemoryTaskStore
from workpanda.infrastructure.octop_runtime import OctopHarnessExecutor, OctopHarnessGateway
from workpanda.infrastructure.rate_limit import TokenBucketLimiter
from workpanda.infrastructure.sqlite_cancel_store import SqliteCancelSignalStore
from workpanda.infrastructure.sqlite_checkpoint_store import SqliteCheckpointStore
from workpanda.infrastructure.sqlite_store import SqliteStore
from workpanda.infrastructure.sqlite_trace_store import SqliteTraceStore


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
    signer: SessionSigner
    limiter: TokenBucketLimiter
    traces: TraceStore
    audit: AuditJournal
    # 取消登记表由容器持有：取消方与执行方看到的必须是同一个对象。
    cancellations: CancelRegistry
    # 工具线程台账：超时后仍在跑的线程数。只有 harness 分支才有，其余为 None。
    tool_threads: Callable[[], dict[str, int]] | None = None
    closers: list = field(default_factory=list)

    def close(self) -> None:
        for closer in self.closers:
            closer()


def resolve_trace_backend(settings: Settings) -> str:
    """决定 trace 落到哪里。auto 跟随任务存储：任务持久化时留痕也持久化。"""

    backend = settings.harness_trace_backend.strip().lower()
    if backend == "auto":
        return "sqlite" if settings.store_backend == "sqlite" else "memory"
    if backend not in {"memory", "sqlite"}:
        # 配置写错时宁可启动失败，也不要静默退化成"重启即丢"。
        raise ValueError(f"unsupported harness_trace_backend: {settings.harness_trace_backend}")
    return backend


def build_trace_store(settings: Settings) -> tuple[TraceStore, list[Callable[[], None]]]:
    if resolve_trace_backend(settings) == "sqlite":
        store = SqliteTraceStore(settings.sqlite_path, capacity=settings.harness_trace_capacity)
        return store, [store.close]
    return InMemoryTraceStore(capacity=max(1, settings.harness_trace_capacity)), []


def build_audit_journal(
    settings: Settings,
) -> tuple[AuditJournal, list[Callable[[], None]]]:
    """审计归档。默认关闭：未配置路径时不落任何文件，避免无意中把痕迹写到意外位置。"""

    path = settings.harness_audit_path
    if path is None:
        return NullAuditJournal(), []
    journal = JsonlAuditJournal(
        path,
        max_bytes=settings.harness_audit_max_bytes,
        backups=settings.harness_audit_backups,
    )
    return journal, []


def build_checkpoint_store(
    settings: Settings,
) -> tuple[CheckpointStore | None, list[Callable[[], None]]]:
    """断点存储。未开启续跑时既不建库也不落盘，避免留下无人使用的会话原文。"""

    if not settings.harness_resume_enabled:
        return None, []
    if settings.store_backend == "sqlite":
        store = SqliteCheckpointStore(settings.sqlite_path)
        return store, [store.close]
    return InMemoryCheckpointStore(), []


def build_lease_store(
    settings: Settings,
) -> tuple[StepLeaseStore | None, list[Callable[[], None]]]:
    """步骤租约。默认关闭：单进程顺序执行不需要，多进程试点显式开启。

    开启时后端跟随任务存储：SQLite 与任务库同文件，因此同机跨进程互斥；
    内存后端只挡同一进程内的并发。跨主机多实例仍需换共享锁。
    """

    if not settings.harness_lease_enabled:
        return None, []
    if settings.store_backend == "sqlite":
        store = SqliteStepLeaseStore(settings.sqlite_path)
        return store, [store.close]
    return InMemoryStepLeaseStore(), []


def build_cancel_registry(
    settings: Settings,
) -> tuple[CancelRegistry, list[Callable[[], None]]]:
    """取消登记表。默认只在进程内可见，多实例试点时切到 SQLite 共享信号。

    共享信号写在任务库同一个文件里，因此同机多进程不需要额外部署；
    写不成功时登记表会抛错，调用方不能把"没送达"当成"已取消"。
    """

    backend = settings.harness_cancel_backend.strip().lower()
    kwargs = {
        "pending_ttl_seconds": settings.harness_cancel_ttl_seconds,
        "poll_interval": settings.harness_cancel_poll_seconds,
        "signal_ttl_seconds": settings.harness_cancel_ttl_seconds,
    }
    if backend == "sqlite":
        shared = SqliteCancelSignalStore(settings.sqlite_path)
        return CancelRegistry(shared=shared, **kwargs), [shared.close]
    if backend != "memory":
        # 配置写错时宁可启动失败：静默退化会让多进程部署以为取消是共享的。
        raise ValueError(f"unsupported harness_cancel_backend: {settings.harness_cancel_backend}")
    return CancelRegistry(**kwargs), []


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
    # 取消默认只在进程内可见；多实例时由配置切到共享信号（见 build_cancel_registry）。
    cancellations, cancel_closers = build_cancel_registry(settings)
    closers.extend(cancel_closers)
    # trace 独立于任务结果存放：既能覆盖成功执行，也能做跨任务的聚合指标。
    traces, trace_closers = build_trace_store(settings)
    closers.extend(trace_closers)
    # 审计归档与 trace 并行存在：一个会被淘汰，一个不会。
    audit, audit_closers = build_audit_journal(settings)
    closers.extend(audit_closers)

    # 只有 harness 分支自己起工具线程，台账也只在那里存在。
    tool_threads: Callable[[], dict[str, int]] | None = None

    if executor is None:
        # 优先级：通用 harness > Octop 运行时 > 脚手架执行器。
        # harness 用的是我们自己的受控循环（工具白名单 + 预算 + 留痕），
        # Octop 运行时作为备选：它能执行步骤，但工具循环不由我们控制。
        if settings.harness_enabled and settings.model_configured:
            tool_registry, tool_closers = default_registry(
                allowed_hosts=settings.allowed_hosts,
                max_stuck_threads=settings.harness_max_stuck_threads,
            )
            tool_threads = tool_registry.threads.stats
            model_client = OpenAICompatibleClient(
                base_url=settings.model_base_url or "",
                api_key=settings.model_api_key or "",
                model=settings.model_name,
                timeout_seconds=settings.model_timeout_seconds,
                trust_env=settings.model_trust_env,
            )
            checkpoints, checkpoint_closers = build_checkpoint_store(settings)
            leases, lease_closers = build_lease_store(settings)
            executor_instance: Executor = HarnessExecutor(
                model_client,
                registry=tool_registry,
                max_steps=settings.harness_max_steps,
                max_tool_calls=settings.harness_max_tool_calls,
                max_tokens=settings.harness_max_tokens,
                deadline_seconds=settings.harness_deadline_seconds,
                tool_retries=settings.harness_tool_retries,
                tool_concurrency=settings.harness_tool_concurrency,
                model_retries=settings.model_retries,
                model_retry_backoff_seconds=settings.model_retry_backoff_seconds,
                redactor=DEFAULT_REDACTOR if settings.harness_redact else NO_REDACTION,
                traces=traces,
                audit=audit,
                checkpoints=checkpoints,
                resume_enabled=settings.harness_resume_enabled,
                resume_ttl_seconds=settings.harness_resume_ttl_seconds,
                leases=leases,
                lease_ttl_seconds=settings.harness_lease_ttl_seconds,
                cancellations=cancellations,
            )
            closers.append(model_client.close)
            closers.extend(tool_closers)
            closers.extend(checkpoint_closers)
            closers.extend(lease_closers)
        elif settings.octop_enabled:
            gateway = OctopHarnessGateway(
                agent_name=settings.octop_agent_name,
                model=settings.octop_model,
                provider=settings.octop_provider,
                api_key=settings.octop_api_key,
                base_url=settings.octop_base_url,
                workspace=settings.octop_workspace,
            )
            # Octop 的工具循环不归本服务控制，读不到取消令牌；至少让"开始前
            # 已经取消"这一步不白跑：包装器统一了所有执行器的第一道边界。
            executor_instance: Executor = CancellableExecutor(
                OctopHarnessExecutor(gateway, timeout_seconds=settings.octop_timeout_seconds),
                cancellations,
            )
            closers.append(gateway.close)
        else:
            # 脚手架执行器同样不读令牌，取消对它就只是账面上的状态翻转。
            executor_instance = CancellableExecutor(ScriptedExecutor(), cancellations)
    else:
        executor_instance = executor

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
        cancellations=cancellations,
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
        signer=SessionSigner(
            settings.session_secret or new_secret(), ttl_seconds=settings.session_ttl_seconds
        ),
        limiter=TokenBucketLimiter(
            per_minute=settings.rate_limit_per_minute, burst=settings.rate_limit_burst
        ),
        traces=traces,
        audit=audit,
        cancellations=cancellations,
        tool_threads=tool_threads,
        closers=closers,
    )
