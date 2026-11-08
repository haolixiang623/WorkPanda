"""Issue #53 的可执行部分：定义并暴露容量指标、以及在启动期堵住「多实例 + 内存后端」。

本模块只做两件事：

1. `validate_deployment_topology`：启动时校验「声明的实例数」与共享状态后端是否自洽。
   服务无法自己数出有几个实例，因此由运维显式声明 `expected_instances`；声明多实例
   却仍用进程内状态后端时启动失败，避免每个实例都以为自己是唯一执行者。
2. `collect_capacity`：采集一组有明确来源的容量指标，供 `/api/ops/metrics` 暴露。

刻意不做、也不假装做了的事：

- **不定义阈值、不判断「是否该扩容」、不触发任何动作**。阈值与升级顺序属于业务/
  安全责任人尚未确认的决策（Issue #53 影响范围已勾选「需要业务或安全责任人确认」）；
  它们写在 `docs/CAPACITY-RUNBOOK.md`，标注为待确认的工程初值，代码不把它们变成规则。
- **采集不到就如实说明原因，绝不返回 0**。0 是一个「看起来正常」的数值，会让值班
  把「没采到」误读成「没有压力」。四种状态分开报：`ok` / `not_applicable`（当前形态
  本来就没有）/ `not_instrumented`（尚未埋点）/ `error`（采集故障）。
- **只输出数值与配置枚举**，绝不携带凭据、连接串、任务正文或群众原话。异常只保留
  类型名，因为 exception message 可能含路径、连接串或业务值。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from workpanda.config import Settings
from workpanda.harness.trace import TraceQuery

MetricStatus = Literal["ok", "not_applicable", "not_instrumented", "error"]


class DeploymentConfigError(ValueError):
    """声明的实例数与共享状态后端不自洽，启动即失败，绝不静默按单实例运行。"""


# 多实例必须跨进程共享的状态后端。sqlite 只在「同机多进程共用一个库文件」时成立，
# 是单机的特例，不接受它作为多实例共享后端。后端取值本身的合法性仍由
# bootstrap.build_cancel_registry 校验，这里不重复、不改变既有错误信息。
MULTI_INSTANCE_SHARED_BACKEND = "redis"


def validate_deployment_topology(settings: Settings) -> None:
    """启动期一致性门禁：声明多实例时，共享状态后端必须跨进程可见。

    单实例（默认）不受任何影响，`memory` / `sqlite` / `redis` 都允许——单实例下
    进程内状态没有第二个执行者来竞争。只有声明 `expected_instances > 1` 才要求
    `redis`，并顺带要求步骤租约（否则同一 (task, step) 会被两个实例各执行一次）。

    不放松既有 fail-closed：选了 redis 而 `RedisClient.ping()` 连不上时，仍由
    `bootstrap.build_shared_state` 抛错终止启动，本函数不会把它「放宽」成警告。
    """

    instances = settings.expected_instances
    if instances < 1:
        raise DeploymentConfigError(
            f"WORKPANDA_EXPECTED_INSTANCES 必须 >= 1，当前为 {instances}；"
            "实例数是运维对部署形态的显式声明，不能省略或置 0。"
        )

    backend = settings.shared_backend
    if instances == 1:
        return

    problems: list[str] = []
    if backend != MULTI_INSTANCE_SHARED_BACKEND:
        problems.append(
            f"共享状态后端为 {backend!r}，进程内/单机状态在实例之间不可见；"
            f"必须设为 {MULTI_INSTANCE_SHARED_BACKEND!r}（并配置可用的 Redis）"
        )
    if not settings.harness_lease_enabled:
        problems.append(
            "步骤租约未开启（harness_lease_enabled=false），同一 (task, step) "
            "可能被多个实例并发执行；需开启租约并显式设置足够长的 TTL"
        )
    if settings.store_backend == "memory":
        problems.append(
            "任务存储为 memory，任务与确认凭证只在各自进程内；多实例必须至少用 sqlite"
            "（跨主机多实例按 Issue #34 迁 PostgreSQL 后才成立）"
        )

    if problems:
        raise DeploymentConfigError(
            f"声明 {instances} 个实例，但部署形态不自洽："
            + "；".join(problems)
            + "。启动失败：多实例而共享后端仍在进程内，会让每个实例都以为自己是唯一执行者。"
        )


def deployment_view(settings: Settings) -> dict[str, Any]:
    """指标端点里的部署形态片段。只输出配置枚举与布尔值，不含连接串/凭据。"""

    return {
        "expected_instances": settings.expected_instances,
        "store_backend": settings.store_backend,
        "shared_backend": settings.shared_backend,
        "lease_enabled": settings.harness_lease_enabled,
        # 供值班核对「这份配置是否声称自己是多实例」，而不是替谁做扩容决策。
        "requires_shared_state": settings.expected_instances > 1,
    }


class MetricValue(BaseModel):
    """单个容量指标。

    只有 `status="ok"` 才带数值；其余三种状态 `value` 一律为 `None` 且必须给出
    `reason`。这条不变量由校验器固化：宁可让人看到「这项没采到」，也不能给一个 0
    让它被当成「一切正常」。
    """

    status: MetricStatus
    available: bool
    value: float | None = None
    unit: str
    reason: str | None = None
    source: str

    @model_validator(mode="after")
    def _value_matches_status(self) -> MetricValue:
        if self.status == "ok":
            if self.value is None or not self.available:
                raise ValueError("ok 指标必须给出实测数值并标记 available")
        else:
            if self.value is not None or self.available:
                raise ValueError("非 ok 指标不得携带数值（尤其不能用 0 冒充正常）")
            if not self.reason:
                raise ValueError("非 ok 指标必须说明原因")
        return self


def measured(*, value: float, unit: str, source: str) -> MetricValue:
    return MetricValue(status="ok", available=True, value=value, unit=unit, source=source)


def unmeasured(*, status: MetricStatus, reason: str, unit: str, source: str) -> MetricValue:
    return MetricValue(
        status=status, available=False, value=None, unit=unit, reason=reason, source=source
    )


class CapacitySnapshot(BaseModel):
    """`/api/ops/metrics` 的响应契约。"""

    deployment: dict[str, Any]
    metrics: dict[str, MetricValue]
    # 只有 error 才是「采集故障」；not_applicable 是当前形态下的正常情况，不算退化。
    degraded: bool = Field(description="存在 error 指标；此时这份快照有缺口，不可当作完整基线。")
    instrumentation_gaps: list[str] = Field(
        default_factory=list, description="尚未埋点的指标名，需按 runbook 补齐后才能标定阈值。"
    )
    not_applicable: list[str] = Field(
        default_factory=list, description="当前部署形态下无数据来源的指标名（正常，不是故障）。"
    )
    note: str = Field(
        default=(
            "阈值与升级顺序见 docs/CAPACITY-RUNBOOK.md，属待业务/安全责任人确认的工程初值；"
            "本端点只暴露实测数值，不判断是否需要扩容、不触发任何动作。"
        )
    )


# 指标名 -> (单位, 采集来源说明)。尚未埋点的项也在此登记，保证契约稳定：
# 值班能看见「有这项指标但当前采不到」，而不是发现它悄悄消失了。
METRIC_SPECS: dict[str, tuple[str, str]] = {
    "sse_connections": ("connections", "EventBroker.total_subscriber_count()"),
    "trace_resident": ("records", "len(traces)"),
    "trace_unconfirmed_rate": ("ratio", "traces.query_summary(TraceQuery()).unconfirmed_rate"),
    "database_file_bytes": ("bytes", "sqlite_path.stat().st_size（仅 sqlite 后端）"),
    "concurrent_tool_threads": ("threads", "tool_threads().live（仅启用 harness 时）"),
    "sqlite_write_lock_wait_ms": ("ms", "尚未埋点：SQLite 写锁等待次数/耗时"),
    "task_create_p95_ms": ("ms", "尚未埋点：任务创建延迟分位数"),
}


def collect_capacity(container: Any) -> CapacitySnapshot:
    """从容器采集容量快照。每一项独立 try，单项失败不影响其它指标。

    采集逻辑只读，不推进流程、不写状态、不发起外部调用。抛异常的采集器一律降级为
    `error` 并带上异常类型名，绝不退化成 0。
    """

    settings: Settings = container.settings
    metrics: dict[str, MetricValue] = {}

    for name, (unit, source) in METRIC_SPECS.items():
        metrics[name] = _collect_one(name, unit, source, container, settings)

    return CapacitySnapshot(
        deployment=deployment_view(settings),
        metrics=metrics,
        degraded=any(item.status == "error" for item in metrics.values()),
        instrumentation_gaps=sorted(
            name for name, item in metrics.items() if item.status == "not_instrumented"
        ),
        not_applicable=sorted(
            name for name, item in metrics.items() if item.status == "not_applicable"
        ),
    )


class _NotApplicable(Exception):
    """当前部署形态下这项指标本来就没有数据来源。"""


class _NotInstrumented(Exception):
    """指标已登记但当前代码没有埋点，明确告知而不是填 0。"""


_NOT_INSTRUMENTED_REASON = {
    "sqlite_write_lock_wait_ms": (
        "SQLite 写锁等待尚未埋点；需在 store.commit 的 busy_timeout 分支累计等待次数与耗时，"
        "见 runbook「埋点缺口」"
    ),
    "task_create_p95_ms": (
        "任务创建 p95 尚未埋点；需在 create_task 出/入口记录耗时分位数，见 runbook「埋点缺口」"
    ),
}


def _collect_one(
    name: str, unit: str, source: str, container: Any, settings: Settings
) -> MetricValue:
    """采集单个指标；任何异常都转成「error + 原因」，不吞成 0。"""

    try:
        value = _read_metric(name, container, settings)
    except _NotInstrumented as exc:
        return unmeasured(
            status="not_instrumented", reason=str(exc), unit=unit, source=source
        )
    except _NotApplicable as exc:
        return unmeasured(status="not_applicable", reason=str(exc), unit=unit, source=source)
    except Exception as exc:  # noqa: BLE001 - 降级为 error，正是这里的目的
        # 只带异常类型名，不带 message：后者可能含路径、连接串或业务值。
        return unmeasured(
            status="error",
            reason=f"采集失败（{type(exc).__name__}），已按不可用上报",
            unit=unit,
            source=source,
        )
    return measured(value=float(value), unit=unit, source=source)


def _read_metric(name: str, container: Any, settings: Settings) -> float:
    """返回实测值；无来源或未埋点用异常区分，绝不用 0 代替。"""

    if name in _NOT_INSTRUMENTED_REASON:
        raise _NotInstrumented(_NOT_INSTRUMENTED_REASON[name])

    if name == "sse_connections":
        return float(container.broker.total_subscriber_count())

    if name == "trace_resident":
        return float(len(container.traces))

    if name == "trace_unconfirmed_rate":
        return float(container.traces.query_summary(TraceQuery()).unconfirmed_rate)

    if name == "database_file_bytes":
        if settings.store_backend != "sqlite":
            raise _NotApplicable("任务存储为 memory，不存在单库文件；切到 sqlite 后才可观测")
        path = settings.sqlite_path
        if not path.is_file():
            raise _NotApplicable("SQLite 文件尚未创建（还没有任务落盘）")
        return float(path.stat().st_size)

    if name == "concurrent_tool_threads":
        stats = container.tool_threads
        if stats is None:
            raise _NotApplicable("未启用 harness，进程内没有工具线程台账")
        return float(stats()["live"])

    raise _NotInstrumented(f"未知指标 {name}")  # pragma: no cover - 与 METRIC_SPECS 同源
