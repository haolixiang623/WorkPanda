"""应用装配：把配置、存储、适配器和应用服务组合成可运行容器。

测试通过替换这里的依赖来构造不同的存储或执行器，而不修改业务代码。
"""

from collections.abc import Callable
from dataclasses import dataclass, field

from workpanda.api.ops_auth import OpsAccessPolicy, build_ops_policy
from workpanda.application.admin_flows import AdminFlowService
from workpanda.application.intent_eval import IntentEvalRunner, IntentReleaseGate
from workpanda.application.ports import Executor, IntentRouter
from workpanda.application.release import FlowReleaseService
from workpanda.application.replay import FlowReplay
from workpanda.application.runner import FlowRunner
from workpanda.application.task_service import TaskService
from workpanda.config import Settings, get_settings
from workpanda.domain.flow import FlowDefinition, FlowRegistry, FlowStore, FlowVersionExists
from workpanda.domain.identity import SessionSigner, new_secret
from workpanda.domain.intent import IntentRule, RuleIntentRouter
from workpanda.domain.replay import ReplayCaseStore
from workpanda.domain.repository import ConfirmationStore, TaskStore
from workpanda.harness.audit import AuditJournal, JsonlAuditJournal, NullAuditJournal
from workpanda.harness.budget import BudgetLedger
from workpanda.harness.builtin import default_registry
from workpanda.harness.cancel import CancelRegistry
from workpanda.harness.cancel_executor import CancellableExecutor
from workpanda.harness.checkpoint import CheckpointStore, InMemoryCheckpointStore
from workpanda.harness.executor import HarnessExecutor, auto_lease_ttl
from workpanda.harness.lease import (
    InMemoryStepLeaseStore,
    SqliteStepLeaseStore,
    StepLeaseStore,
)
from workpanda.harness.openai_client import OpenAICompatibleClient
from workpanda.harness.redaction import DEFAULT_REDACTOR, NO_REDACTION
from workpanda.harness.shared_state import SharedStateStore
from workpanda.harness.trace import InMemoryTraceStore, TraceStore
from workpanda.infrastructure.adapters import ScriptedExecutor
from workpanda.infrastructure.demo_flows import demo_flows, demo_intent_rules
from workpanda.infrastructure.event_broker import EventBroker
from workpanda.infrastructure.memory_store import (
    InMemoryConfirmationStore,
    InMemoryFlowStore,
    InMemoryTaskStore,
)
from workpanda.infrastructure.rate_limit import TokenBucketLimiter
from workpanda.infrastructure.redis_client import RedisClient
from workpanda.infrastructure.redis_shared_state import (
    RedisBudgetLedger,
    RedisCancelSignalStore,
    RedisSharedState,
    RedisStepLeaseStore,
)
from workpanda.infrastructure.replay_cases import (
    InMemoryReplayCaseStore,
    SqliteReplayCaseStore,
)
from workpanda.infrastructure.sqlite_cancel_store import SqliteCancelSignalStore
from workpanda.infrastructure.sqlite_checkpoint_store import SqliteCheckpointStore
from workpanda.infrastructure.sqlite_flow_store import SqliteFlowStore
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
    # 运维接口的凭据与角色。构造时就校验配置，写错则启动失败。
    ops_access: OpsAccessPolicy
    # 管理侧配置台的凭据与角色，与运维凭据完全独立（见 api/admin.py）。
    admin_access: OpsAccessPolicy
    traces: TraceStore
    audit: AuditJournal
    # 取消登记表由容器持有：取消方与执行方看到的必须是同一个对象。
    cancellations: CancelRegistry
    # 工具线程台账：超时后仍在跑的线程数。只有 harness 分支才有，其余为 None。
    tool_threads: Callable[[], dict[str, int]] | None = None
    # 流程版本库。测试注入定义时为 None（那些定义不落库）。
    flow_store: FlowStore | None = None
    # 回放用例库。与流程版本同后端：用例跟着版本走，换后端不该丢基线。
    replay_cases: ReplayCaseStore | None = None
    # 发布门禁。没有版本库时无从裁决，为 None。
    release: FlowReleaseService | None = None
    # 管理侧配置台服务。同样只在有版本库时成立（注入定义时没有版本库）。
    admin_flows: AdminFlowService | None = None
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


def build_flow_store(settings: Settings) -> tuple[FlowStore, list[Callable[[], None]]]:
    """流程版本库。跟随任务存储：sqlite 时与任务库同文件，memory 时进程内。

    同文件而非独立库：发布是"写版本 + 移指针"两步，同库才能一次事务收口。
    """

    if settings.store_backend == "sqlite":
        store = SqliteFlowStore(settings.sqlite_path)
        return store, [store.close]
    return InMemoryFlowStore(), []


def build_replay_case_store(
    settings: Settings,
) -> tuple[ReplayCaseStore, list[Callable[[], None]]]:
    """回放用例库。与流程版本同后端：用例跟着版本走，换后端不该丢基线。

    sqlite 时同库同文件：发布门禁要"读版本 + 读用例"，同一个连接才能避免读到
    两个时刻的库。
    """

    if settings.store_backend == "sqlite":
        store = SqliteReplayCaseStore(settings.sqlite_path)
        return store, [store.close]
    return InMemoryReplayCaseStore(), []


def seed_demo_flows(store: FlowStore, *, enabled: bool) -> None:
    """把骨架自检流程同步到库里的真实状态。

    开启时确保版本存在、指针指向它；关闭时**清空指针**。只"不写入"是不够的：
    上一次开着跑过的库里还留着指针，那等于关不掉——而"正式接入业务前必须关闭"
    这条要求必须真的生效。

    已存在的同版本内容一律沿用、不覆盖：版本不可变是回滚能成立的前提。
    """

    for flow in demo_flows():
        if not enabled:
            store.disable(flow.flow_id)
            continue
        try:
            store.add_version(flow)
        except FlowVersionExists:
            pass
        store.publish(flow.flow_id, flow.version)


def build_shared_state(
    settings: Settings,
) -> tuple[RedisSharedState | None, list[Callable[[], None]]]:
    """共享状态后端。只有显式选 redis 时才建立连接，其余后端返回 None。

    启动时先探活：配置成共享、实际连不上时，宁可起不来也不能让每个实例
    各自以为"自己是唯一的执行者"。
    """

    if settings.shared_backend != "redis":
        return None, []
    url = (settings.harness_redis_url or "").strip()
    if not url:
        raise ValueError(
            "harness_shared_backend=redis 时必须配置 harness_redis_url"
            "（形如 redis://:密码@主机:6379/0）"
        )
    client = RedisClient(url, timeout_seconds=settings.harness_redis_timeout_seconds)
    client.ping()
    state = RedisSharedState(client, prefix=settings.harness_shared_key_prefix)
    return state, [state.close]


def build_lease_store(
    settings: Settings,
    shared: SharedStateStore | RedisSharedState | None = None,
) -> tuple[StepLeaseStore | None, list[Callable[[], None]]]:
    """步骤租约。默认关闭：单进程顺序执行不需要，多进程试点显式开启。

    开启时后端跟随共享配置：redis（跨主机）、SQLite（同机跨进程，与任务库同文件）、
    内存（只挡同一进程内的并发）。
    """

    if not settings.harness_lease_enabled:
        return None, []
    if isinstance(shared, RedisSharedState):
        return RedisStepLeaseStore(shared), []
    if shared is not None:
        raise ValueError("步骤租约只支持 redis 共享后端；其余共享实现尚未提供租约语义")
    if settings.store_backend == "sqlite":
        store = SqliteStepLeaseStore(settings.sqlite_path)
        return store, [store.close]
    return InMemoryStepLeaseStore(), []


def build_cancel_registry(
    settings: Settings,
    shared: SharedStateStore | RedisSharedState | None = None,
) -> tuple[CancelRegistry, list[Callable[[], None]]]:
    """取消登记表。默认只在进程内可见，多实例试点时切到共享信号。

    SQLite 写在任务库同一个文件里，同机多进程不需要额外部署；redis 供跨主机使用。
    写不成功时登记表会抛错，调用方不能把"没送达"当成"已取消"。
    """

    backend = settings.shared_backend
    kwargs = {
        "pending_ttl_seconds": settings.harness_cancel_ttl_seconds,
        "poll_interval": settings.harness_cancel_poll_seconds,
        "signal_ttl_seconds": settings.harness_cancel_ttl_seconds,
    }
    if backend == "sqlite":
        store = SqliteCancelSignalStore(settings.sqlite_path)
        return CancelRegistry(shared=store, **kwargs), [store.close]
    if backend == "redis":
        if not isinstance(shared, RedisSharedState):
            raise ValueError("取消信号后端为 redis，但没有可用的共享连接（检查 harness_redis_url）")
        return CancelRegistry(shared=RedisCancelSignalStore(shared), **kwargs), []
    if backend != "memory":
        # 配置写错时宁可启动失败：静默退化会让多进程部署以为取消是共享的。
        raise ValueError(f"unsupported harness_shared_backend: {settings.harness_shared_backend}")
    return CancelRegistry(**kwargs), []


def build_budget_ledger(
    settings: Settings,
    shared: SharedStateStore | RedisSharedState | None = None,
) -> tuple[BudgetLedger | None, list[Callable[[], None]]]:
    """跨主机预算账本。默认 None：预算仍在单进程内累计，行为与之前一致。

    TTL 与租约同口径（deadline + 120s 余量）：账本在执行中途过期等于配额被重置，
    比多算一点危险得多。连接由共享状态持有，这里不再单独管理生命周期。
    """

    if not isinstance(shared, RedisSharedState):
        return None, []
    ttl = settings.harness_budget_ttl_seconds or auto_lease_ttl(settings.harness_deadline_seconds)
    return RedisBudgetLedger(shared, ttl_seconds=ttl), []


def default_intent_rules(flow_definitions: list[FlowDefinition]) -> tuple[IntentRule, ...]:
    """按已注册流程裁剪默认意图规则。

    规则指向未注册的流程时，识别"成功"也会在解析流程那一步变成 422——那看起来
    像群众说错了话，实际是装配错了。因此在装配期就裁掉，不留给运行时。
    """

    registered = {(flow.flow_id, flow.version) for flow in flow_definitions}
    return tuple(
        rule for rule in demo_intent_rules() if (rule.flow_id, rule.flow_version) in registered
    )


def build_intent_router(flow_definitions: list[FlowDefinition]) -> RuleIntentRouter:
    """装配识别路由器。服务端与运维脚本共用这一个函数，避免两处规则口径不同。

    评测必须评"线上真的会用的那个路由器"：如果 CI 自己拼一台路由器，评测通过
    只能证明评测代码对，证明不了服务能听懂群众说话。
    """

    return RuleIntentRouter(rules=default_intent_rules(flow_definitions))


def build_container(
    settings: Settings | None = None,
    *,
    flows: list[FlowDefinition] | None = None,
    executor: Executor | None = None,
    router: IntentRouter | None = None,
) -> Container:
    settings = settings or get_settings()
    # 凭据配置在打开任何存储之前校验：配置写错时不该先占住数据库连接再抛错。
    ops_access = build_ops_policy(settings.ops_token, settings.ops_tokens)
    # 管理侧只接受「主体:角色:密钥」多主体格式，不引入 legacy 单密钥：
    # 能改流程语义的凭据必须能定位到具体主体。配置为空时接口整体关闭。
    admin_access = build_ops_policy(None, settings.admin_tokens)

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

    if flows is not None:
        # 测试与嵌入场景直接给定义：不落库，也不碰库里已有的版本。
        registry = FlowRegistry(flows)
        flow_store: FlowStore | None = None
    else:
        # 流程定义以库为准：进程内不再有"代码里那一份"作为第二个真相。
        flow_store, flow_closers = build_flow_store(settings)
        closers.extend(flow_closers)
        seed_demo_flows(flow_store, enabled=settings.enable_demo_flow)
        registry = FlowRegistry.from_store(flow_store)
    replay_cases, replay_closers = build_replay_case_store(settings)
    closers.extend(replay_closers)
    registered = registry.versions()
    # 未显式注入路由器时用确定性规则路由器：没有规则就等于"识别不了"，
    # 让创建任务走澄清而不是猜一个流程。它在发布门禁之前装配，因为门禁要评的
    # 正是"线上会用的这个路由器"。
    router_instance = router or build_intent_router(registered)
    # 识别样例门禁只在有版本库时成立：注入定义的场景没有"发布"这回事。
    intent_gate = (
        IntentReleaseGate(
            router=router_instance,
            runner=IntentEvalRunner(timeout_seconds=settings.intent_eval_timeout_seconds),
            min_positive=settings.intent_eval_min_positive,
            min_near_miss=settings.intent_eval_min_near_miss,
            max_miss_rate=settings.intent_eval_max_miss_rate,
        )
        if flow_store is not None and settings.intent_sample_gate
        else None
    )
    # 发布门禁只在有版本库时成立：注入定义的那些定义不在库里，"发布"无从谈起。
    release = (
        FlowReleaseService(
            flows=flow_store,
            cases=replay_cases,
            replay=FlowReplay(timeout_seconds=settings.replay_timeout_seconds),
            gate_enabled=settings.replay_release_gate,
            intent_gate=intent_gate,
        )
        if flow_store is not None
        else None
    )
    # 管理侧服务与发布门禁共用同一份版本库与用例库：配置台看到的版本、门禁
    # 裁决时读的用例、发布后 registry 重载的版本，必须是同一份状态。
    admin_flows = (
        AdminFlowService(
            flows=flow_store,
            cases=replay_cases,
            release=release,
            registry=registry,
        )
        if flow_store is not None and release is not None
        else None
    )
    broker = EventBroker()
    # 共享状态后端先建：取消信号、步骤租约、预算账本三处共用同一个连接与键前缀。
    shared_state, shared_closers = build_shared_state(settings)
    closers.extend(shared_closers)
    # 取消默认只在进程内可见；多实例时由配置切到共享信号（见 build_cancel_registry）。
    cancellations, cancel_closers = build_cancel_registry(settings, shared_state)
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
        # 真实执行路径只有内置 harness 一条：工具白名单、预算、留痕、取消都在我们
        # 自己的循环里，才能做到「每次工具调用」级别的受控。另一个分支是脚手架占位
        # （ScriptedExecutor），它不跑模型循环，只用于验证任务链路。
        #
        # 曾经存在的 Octop 运行时备选已移除：把整步委托出去后，工具循环不在本服务
        # 内，取消、预算、留痕、确认都只能落到「整步」粒度，无法证明某次副作用是否
        # 发生。这属于能力边界，不是配置选项。
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
            leases, lease_closers = build_lease_store(settings, shared_state)
            budget_ledger, budget_closers = build_budget_ledger(settings, shared_state)
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
                budget_ledger=budget_ledger,
            )
            closers.append(model_client.close)
            closers.extend(tool_closers)
            closers.extend(checkpoint_closers)
            closers.extend(lease_closers)
            closers.extend(budget_closers)
        else:
            # 脚手架执行器不读取消令牌，取消对它就只是账面上的状态翻转。
            executor_instance = CancellableExecutor(ScriptedExecutor(), cancellations)
    else:
        executor_instance = executor

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
        ops_access=ops_access,
        admin_access=admin_access,
        traces=traces,
        audit=audit,
        cancellations=cancellations,
        tool_threads=tool_threads,
        flow_store=flow_store,
        replay_cases=replay_cases,
        release=release,
        admin_flows=admin_flows,
        closers=closers,
    )
