"""应用装配：把配置、存储、适配器和应用服务组合成可运行容器。

测试通过替换这里的依赖来构造不同的存储或执行器，而不修改业务代码。
"""

from collections.abc import Callable
from dataclasses import dataclass, field

from workpanda.api.ops_auth import OpsAccessPolicy, build_ops_policy
from workpanda.application.admin_flows import AdminFlowService
from workpanda.application.deeplink_confirm import ConfirmationLinkService
from workpanda.application.im_cards import CardQueue, NotifyingEventPublisher
from workpanda.application.im_gateway import ImGatewayService, ReadOnlyTasks
from workpanda.application.intent_eval import IntentEvalRunner, IntentReleaseGate
from workpanda.application.intent_routing import RegistryIntentRouter, router_for
from workpanda.application.ports import Executor, IntentRouter
from workpanda.application.release import FlowReleaseService
from workpanda.application.replay import FlowReplay
from workpanda.application.runner import FlowRunner
from workpanda.application.staff_access import ImIdentityResolver, StaffDirectoryService
from workpanda.application.task_service import TaskService
from workpanda.capacity import validate_deployment_topology
from workpanda.config import Settings, get_settings
from workpanda.domain.deeplink import ConfirmationLinkStore
from workpanda.domain.flow import FlowDefinition, FlowRegistry, FlowStore, FlowVersionExists
from workpanda.domain.identity import SessionSigner, new_secret
from workpanda.domain.im_gateway import CardSender, ImGatewayStore
from workpanda.domain.intent import RuleIntentRouter
from workpanda.domain.replay import ReplayCaseStore
from workpanda.domain.repository import ConfirmationStore, TaskStore
from workpanda.domain.staff import STAFF_ROLE, StaffStore
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
from workpanda.harness.preflight import ensure_step_tools_available
from workpanda.harness.redaction import DEFAULT_REDACTOR, NO_REDACTION
from workpanda.harness.shared_state import SharedStateStore
from workpanda.harness.trace import InMemoryTraceStore, TraceStore
from workpanda.infrastructure.adapters import ScriptedExecutor
from workpanda.infrastructure.deeplink_store import (
    InMemoryConfirmationLinkStore,
    SqliteConfirmationLinkStore,
)
from workpanda.infrastructure.demo_flows import demo_flows
from workpanda.infrastructure.event_broker import EventBroker
from workpanda.infrastructure.im_gateway_store import (
    InMemoryImGatewayStore,
    SqliteImGatewayStore,
)
from workpanda.infrastructure.memory_store import (
    InMemoryConfirmationStore,
    InMemoryFlowStore,
    InMemoryTaskStore,
)
from workpanda.infrastructure.pilot_flows import pilot_flows
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
from workpanda.infrastructure.staff_store import InMemoryStaffStore, SqliteStaffStore
from workpanda.infrastructure.wecom import (
    WeComCallbackAdapter,
    WeComCardSender,
    WeComConfigError,
    validate_wecom_config,
    validate_wecom_outbound,
)


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
    # 工作人员凭据（只认 `staff` 角色）与工作人员目录。凭据证明"你是谁"，
    # 目录决定"能对哪些流程做什么动作"——两者分开，才能改授权不换凭据。
    staff_access: OpsAccessPolicy
    staff_store: StaffStore
    # IM 身份 → 工作人员的解析器。N4 网关唯一该用的入口，未映射一律拒绝并留痕。
    im_resolver: ImIdentityResolver
    # 卡片深链（N5）。它只做导航，不签发确认凭证；确认仍走 tasks.confirm。
    confirmation_links: ConfirmationLinkService
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
    # 工作人员目录的维护入口（管理侧接口用）。目录本身始终存在。
    staff_directory: StaffDirectoryService | None = None
    # IM 网关（N4）。未配置企业微信回调参数时为 None，`/api/im/*` 整体按不存在处理。
    im_gateway: ImGatewayService | None = None
    # 入站去重与投递留痕。网关没启用时也建着：它是"这条消息处理过没有"的唯一真相，
    # 开关一次不该把历史抹掉。
    im_store: ImGatewayStore | None = None
    # 出站卡片队列。没配发送凭据或站点根时为 None，事件发布直接走总线。
    im_cards: CardQueue | None = None
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


def build_staff_store(settings: Settings) -> tuple[StaffStore, list[Callable[[], None]]]:
    """工作人员目录、IM 映射与解析留痕。跟随任务存储：sqlite 时与任务库同文件。

    与任务库分开存会带来一个假象："映射改了但留痕还在别处"，于是排障时两边
    对不上。同文件才能让"改映射 + 读主体 + 写留痕"落在一个事务语义里。
    """

    if settings.store_backend == "sqlite":
        store = SqliteStaffStore(settings.sqlite_path)
        return store, [store.close]
    return InMemoryStaffStore(), []


def build_confirmation_link_store(
    settings: Settings,
) -> tuple[ConfirmationLinkStore, list[Callable[[], None]]]:
    """卡片深链与留痕。跟随任务存储：sqlite 时与任务库同文件。

    深链本身丢了不影响正确性（旧卡片打不开，重发一张即可），但留痕必须和业务
    数据同库：排障要回答"这张卡发出去没有"，两边分家就永远对不上。
    """

    if settings.store_backend == "sqlite":
        store = SqliteConfirmationLinkStore(settings.sqlite_path)
        return store, [store.close]
    return InMemoryConfirmationLinkStore(), []


def build_im_store(settings: Settings) -> tuple[ImGatewayStore, list[Callable[[], None]]]:
    """入站去重与投递留痕。跟随任务存储：sqlite 时与任务库同文件。

    同库不是为了省事：重启后"这条消息处理过没有"必须还在，否则 IM 重投会被
    当成新消息，重复回卡片。
    """

    if settings.store_backend == "sqlite":
        store = SqliteImGatewayStore(settings.sqlite_path)
        return store, [store.close]
    return InMemoryImGatewayStore(), []


def build_im_gateway(
    settings: Settings,
    *,
    staff_store: StaffStore,
    im_store: ImGatewayStore,
    tasks,
    sender: CardSender | None,
) -> tuple[ImGatewayService | None, list[Callable[[], None]]]:
    """装配 IM 网关。三项回调配置全空时返回 None：`/api/im/*` 整体按不存在处理。

    与 staff 凭据同一个立场——**没有配置就是没有这个入口**，而不是"存在但每次都
    报错"。后者会把一个配置问题变成一串看起来像攻击的 400。
    """

    # 先校验再判"配没配"：半套配置（配了 token 却漏了密钥）必须启动即失败，
    # 而不是被当成"没配置"静默关掉——那是一种没人会发现的失效。
    token, aes_key, corp_id = validate_wecom_config(
        token=settings.wecom_callback_token,
        encoding_aes_key=settings.wecom_encoding_aes_key,
        corp_id=settings.wecom_corp_id,
    )
    # 站点根在这里读一次：格式错误（带路径、漏 scheme）要在启动期就失败，
    # 而不是等第一张卡片发出去才指向一个 404。
    console_base = settings.staff_console_base
    corp_secret, agent_id = validate_wecom_outbound(
        corp_secret=settings.wecom_corp_secret,
        agent_id=settings.wecom_agent_id,
        console_base_url=console_base,
    )
    if not token:
        if corp_secret:
            # 出站只在网关装配之后才可能发生。没有回调配置就没有网关，
            # 此时配了发送凭据只会得到"任务在跑、卡片没有"——按配置错误处理。
            raise WeComConfigError(
                "配置了企业微信出站凭据，但没有回调配置：网关不会装配，卡片发不出去。"
                "请补齐 wecom_callback_token / wecom_encoding_aes_key / wecom_corp_id，"
                "或清空出站凭据。"
            )
        return None, []
    adapter = WeComCallbackAdapter(token=token, encoding_aes_key=aes_key, corp_id=corp_id)
    closers: list[Callable[[], None]] = []
    if sender is None and corp_secret:
        # 出站凭据齐全才建真实发送器；留空表示只接入站，投递记成 skipped。
        real_sender = WeComCardSender(
            corp_id=corp_id,
            corp_secret=corp_secret,
            agent_id=agent_id,
            timeout_seconds=settings.wecom_timeout_seconds,
        )
        sender = real_sender
        closers.append(real_sender.close)
    gateway = ImGatewayService(
        adapter=adapter,
        resolver=ImIdentityResolver(staff_store),
        staff_store=staff_store,
        tasks=tasks,
        store=im_store,
        sender=sender,
        console_base_url=console_base,
    )
    return gateway, closers


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


# 装配期播种的署名。故意不是人名：这一刻没有"某个人"在做，写成人名就是伪造记录。
SEED_ACTOR = "bootstrap"


def seed_demo_flows(store: FlowStore, *, enabled: bool) -> None:
    """把骨架自检流程同步到库里的真实状态。

    开启时确保版本存在、指针指向它；关闭时**停用**它。只"不写入"是不够的：
    上一次开着跑过的库里还留着指针，那等于关不掉——而"正式接入业务前必须关闭"
    这条要求必须真的生效。

    已存在的同版本内容一律沿用、不覆盖：版本不可变是回滚能成立的前提。

    署名固定为 `SEED_ACTOR`：这一步没有"某个人"在做，写成人名就是伪造记录；
    写成一个明确表示"装配期同步"的名字，事后能一眼看出它不是一次人工发布。
    """

    _seed(store, demo_flows(), enabled=enabled)


def seed_pilot_flows(store: FlowStore, *, enabled: bool) -> None:
    """把真实试点事项同步到库里。

    与自检流程用同一套播种语义（见 `seed_demo_flows`），刻意不合并成"一次播种所有
    流程"：两者开关不同、生命周期不同——自检流程在正式接入后必须关掉，试点事项却是
    要开着的。合并之后每次改一个开关都得小心别连带影响另一个。
    """

    _seed(store, pilot_flows(), enabled=enabled)


def _seed(store: FlowStore, flows: list[FlowDefinition], *, enabled: bool) -> None:
    for flow in flows:
        if not enabled:
            store.disable(flow.flow_id, actor=SEED_ACTOR)
            continue
        try:
            store.add_version(flow)
        except FlowVersionExists:
            pass
        store.publish(flow.flow_id, flow.version, actor=SEED_ACTOR)


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


def build_intent_router(flow_definitions: list[FlowDefinition]) -> RuleIntentRouter:
    """按一组流程定义装配识别路由器。服务端与运维脚本共用这一个函数。

    规则现在来自流程版本自身（`FlowDefinition.intent_rules`），所以"传进来的
    定义集合"就是口径本身：运行时传已发布版本、发布门禁传"发布后会生效的那一
    组"。评测必须评"线上真的会用的那个路由器"——如果 CI 自己拼一台路由器，评测
    通过只能证明评测代码对，证明不了服务能听懂群众说话。
    """

    return router_for(flow_definitions)


def build_container(
    settings: Settings | None = None,
    *,
    flows: list[FlowDefinition] | None = None,
    executor: Executor | None = None,
    router: IntentRouter | None = None,
    im_sender: CardSender | None = None,
) -> Container:
    settings = settings or get_settings()
    # 部署形态门禁最先跑：声明多实例却仍用进程内状态时，不能先占住存储/连接
    # 再抛错。它只做纯配置校验，选 redis 后的探活仍由 build_shared_state 负责。
    validate_deployment_topology(settings)
    # 凭据配置在打开任何存储之前校验：配置写错时不该先占住数据库连接再抛错。
    ops_access = build_ops_policy(settings.ops_token, settings.ops_tokens)
    # 管理侧只接受「主体:角色:密钥」多主体格式，不引入 legacy 单密钥：
    # 能改流程语义的凭据必须能定位到具体主体。配置为空时接口整体关闭。
    admin_access = build_ops_policy(None, settings.admin_tokens)
    # 工作人员凭据只接受 `staff` 角色：把它写成 admin 就等于让每个办事员都能改
    # 「哪一步需要群众确认」，而这正是管理侧标为高风险的能力。
    staff_access = build_ops_policy(
        None, settings.staff_tokens, allowed_roles=(STAFF_ROLE,), field="staff_tokens"
    )

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
        seed_pilot_flows(flow_store, enabled=settings.enable_pilot_flows)
        registry = FlowRegistry.from_store(flow_store)
    replay_cases, replay_closers = build_replay_case_store(settings)
    closers.extend(replay_closers)
    # 未显式注入路由器时用跟着发布指针自刷新的规则路由器：没有规则就等于"识别不了"，
    # 让创建任务走澄清而不是猜一个流程。它必须在"读指针"这一步之后建，因为规则
    # 取自当前已发布的版本；之后每次发布都会由 generation 变化触发重建（#73）。
    router_instance = router or RegistryIntentRouter(registry)
    # 识别样例门禁只在有版本库时成立：注入定义的场景没有"发布"这回事。
    #
    # 门禁要评的是"这个版本发布**之后**线上会用的那台路由器"。两种装配制度分开：
    # - 未注入（默认）：路由由流程版本派生（D2=B），发布后就是 `已发布 + 这一版`，
    #   所以传 None 让门禁按待评语料自己装配。此刻运行时的 `RegistryIntentRouter`
    #   还只认得旧版本，拿它去评新版本必然全判漏路由，新版本永远发不上去（#73）；
    # - 显式注入：路由由调用方掌握，发布不会改变它，发布后仍是这一台，那就评它。
    intent_gate = (
        IntentReleaseGate(
            router=router,
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
    # 工作人员目录始终存在（与版本库不同）：它只描述人，不依赖流程是否存在。
    staff_store, staff_closers = build_staff_store(settings)
    closers.extend(staff_closers)
    staff_directory = StaffDirectoryService(staff_store)
    im_resolver = ImIdentityResolver(staff_store)
    # 深链存储与任务库同后端，装配点紧挨着工作人员目录：两者都只描述"谁能办事"。
    link_store, link_closers = build_confirmation_link_store(settings)
    closers.extend(link_closers)
    broker = EventBroker()
    # IM 网关只拿只读任务视图（ReadOnlyTasks）：它没有 commit/answer/confirm/cancel
    # 可调用，"网关不会改任务状态"因此是没有入口，而不是自觉。
    im_store, im_store_closers = build_im_store(settings)
    closers.extend(im_store_closers)
    im_gateway, im_gateway_closers = build_im_gateway(
        settings,
        staff_store=staff_store,
        im_store=im_store,
        tasks=ReadOnlyTasks(store),
        sender=im_sender,
    )
    closers.extend(im_gateway_closers)
    # 出站卡片走队列：发送在后台线程里做，流程线程只投一个任务号。缺站点根或
    # 缺发送凭据时不建队列，事件发布直接走总线（少一个线程，也少一类"发了但
    # 打不开"的卡片）。
    im_cards: CardQueue | None = None
    if (
        im_gateway is not None
        and im_gateway.sender is not None
        and im_gateway.console_base_url
    ):
        im_cards = CardQueue(
            im_gateway.notify_task, capacity=settings.im_card_queue_capacity
        )
        closers.append(im_cards.close)
    publisher = NotifyingEventPublisher(broker, im_cards)
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
                browser_policy=settings.browser_policy,
                max_stuck_threads=settings.harness_max_stuck_threads,
            )
            # 流程里声明的工具必须真的在进程里。缺一个就在启动时停下：放过去的话
            # 执行步骤会拿着空授权跑完并报 succeeded（见 harness/preflight.py）。
            # 只核对**已发布**版本——草稿跑不起来不等于装不上。
            ensure_step_tools_available(registry.published(), tool_registry)
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
        publisher=publisher,
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
        staff_access=staff_access,
        staff_store=staff_store,
        im_resolver=im_resolver,
        confirmation_links=ConfirmationLinkService(
            store=store,
            links=link_store,
            ttl_seconds=settings.confirmation_link_ttl_seconds,
            base_url=settings.confirmation_link_base,
        ),
        traces=traces,
        audit=audit,
        cancellations=cancellations,
        tool_threads=tool_threads,
        flow_store=flow_store,
        replay_cases=replay_cases,
        release=release,
        admin_flows=admin_flows,
        staff_directory=staff_directory,
        im_gateway=im_gateway,
        im_store=im_store,
        im_cards=im_cards,
        closers=closers,
    )
