"""执行留痕的独立存储与聚合。

此前 trace 只随任务结果一起存进 `Task.result`，后果是：

- 只能按任务查，无法回答"最近哪些步骤在超预算""哪类工具老被拦"；
- 成功执行的 trace 没有事件承载点，等于不落盘；
- 想加指标就得把全部任务翻一遍。

这里把"一次执行"抽成一条 `TraceRecord`，单独存储并聚合，业务任务状态仍以
Task Store 为准——trace 只描述"执行发生了什么"，不参与流程推进。
"""

from __future__ import annotations

import base64
import json
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from threading import Lock
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, Field

# `canonical_stamp` 由任务、事件、审计几处翻页共用，定义下沉到 domain.paging；
# 这里重新导出，既有 `from workpanda.harness.trace import canonical_stamp` 不变。
from workpanda.domain.paging import canonical_stamp


def _utc_now() -> datetime:
    return datetime.now(UTC)


class TraceRecord(BaseModel):
    """一次执行（一个步骤的一次运行）的摘要留痕。

    字段刻意保持精简：详细报文在任务事件的 `trace` 里，这里只放检索与聚合需要的维度。
    """

    trace_id: str = Field(min_length=1, max_length=64)
    task_id: UUID
    flow_id: str
    flow_version: str
    step_id: str
    outcome: str
    stop_reason: str
    error_code: str | None = None
    risk: str = "low"
    model_calls: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    successful_tool_calls: int = Field(default=0, ge=0)
    blocked_tool_calls: int = Field(default=0, ge=0)
    failed_tool_calls: int = Field(default=0, ge=0)
    timed_out_tool_calls: int = Field(default=0, ge=0)
    # 模型要求过、但因预算用尽或取消没执行的调用。不计入 tool_calls：
    # 那几件"没做"的事要能被单独检索到，而不是混进"做了几次"里。
    skipped_tool_calls: int = Field(default=0, ge=0)
    tokens: int = Field(default=0, ge=0)
    duration_ms: int = Field(default=0, ge=0)
    # 这次执行是从哪个 trace 续跑来的。为空表示从零开始，事后据此判断"重试了几次"。
    resumed_from: str | None = None
    created_at: datetime = Field(default_factory=_utc_now)

    @property
    def created_at_stamp(self) -> str:
        """`created_at` 的规范化文本。排序、翻页游标、存储列都用它。"""

        return canonical_stamp(self.created_at)


class TraceSummary(BaseModel):
    """聚合指标。用于回答"整体是否在退化"，不用于评价单个任务。"""

    total: int = 0
    succeeded: int = 0
    failed: int = 0
    unconfirmed: int = 0
    # 未确认占比是这套系统最关键的健康指标：它代表"说不清是否办成"的比例。
    unconfirmed_rate: float = 0.0
    tool_calls: int = 0
    blocked_tool_calls: int = 0
    failed_tool_calls: int = 0
    timed_out_tool_calls: int = 0
    skipped_tool_calls: int = 0
    avg_model_calls: float = 0.0
    avg_duration_ms: float = 0.0
    tokens_total: int = 0


class TraceStore(Protocol):
    """留痕存储的统一接口。

    `list` / `summary` 是稳定的小接口（旧的调用点、测试都用它们）；
    `query` / `query_summary` 是带完整条件的版本，两套后端都要实现，
    且必须给出逐条一致的结果——分页翻出来的记录集合不允许依赖后端选择。
    """

    def save(self, record: TraceRecord) -> None: ...

    def list(self, *, task_id: UUID | None = None, limit: int = 50) -> list[TraceRecord]: ...

    def query(self, query: TraceQuery) -> TracePage: ...

    def summary(self, *, task_id: UUID | None = None) -> TraceSummary: ...

    def query_summary(self, query: TraceQuery) -> TraceSummary: ...


@dataclass(frozen=True, slots=True)
class TraceCursor:
    """键集翻页的锚点：上一页最后一条的 `(created_at, trace_id)`。

    为什么不用 offset：trace 库是"有界 + 持续写入"的——旧记录会被容量淘汰，
    新记录随时插到最前面。offset 在这种库上翻页会**静默漏记录**（淘汰让后面的
    记录整体前移）或**重复记录**（新写入让位置整体后移），而且两种错法都不会报错。
    锚在"具体一条记录"上就没有这个问题：下一页只要求"比这条更旧"。

    `(created_at, trace_id)` 是严格全序：trace_id 唯一，所以同一时刻的多条
    也有确定顺序，两种后端可以给出逐条一致的分页结果。
    """

    created_at: datetime
    trace_id: str

    @property
    def stamp(self) -> str:
        return canonical_stamp(self.created_at)


@dataclass(frozen=True, slots=True)
class TraceQuery:
    """一次 trace 检索的完整条件。分页只是它的一部分。

    时间窗按 `since <= created_at < until`（左闭右开）解释：相邻两个窗口拼起来
    既不会漏一秒，也不会把同一条记录算进两次——做"昨天 / 今天"对比时这一点很要紧。
    """

    task_id: UUID | None = None
    outcome: str | None = None
    stop_reason: str | None = None
    risk: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    limit: int = 50
    cursor: TraceCursor | None = None

    def filters(self) -> dict[str, object]:
        """去掉分页维度后的条件，用于 summary 复用同一组过滤。"""

        return {
            "task_id": self.task_id,
            "outcome": self.outcome,
            "stop_reason": self.stop_reason,
            "risk": self.risk,
            "since": self.since,
            "until": self.until,
        }


class TracePage(BaseModel):
    """一页结果。`next_cursor` 为空表示"到头了"，不是"这一页是空的"。

    刻意不带 `total`：窗口总量由同一组过滤条件下的 `summary().total` 给出，
    两处各算一遍必然漂移——一个按窗口算、一个按页算，迟早出现"说有下一页、
    翻过去是空的"这类对不上的数字。
    """

    items: list[TraceRecord] = Field(default_factory=list)
    # 为空表示"到头了"。游标是不透明字符串，不要手工拼。
    next_cursor: str | None = None


_CURSOR_VERSION = 1


def encode_cursor(cursor: TraceCursor) -> str:
    """把锚点编成不透明字符串。

    刻意做成对调用方不透明：游标是内部位置，不是可以手写的查询参数。
    手搓的游标要么被静默当成第一页（用户以为看全了，其实没有），要么行为未定义。
    """

    payload = json.dumps(
        [_CURSOR_VERSION, cursor.stamp, cursor.trace_id], separators=(",", ":")
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_cursor(raw: str) -> TraceCursor:
    """解析游标。任何不合法都抛 `ValueError`，绝不悄悄退回第一页。"""

    try:
        padding = "=" * (-len(raw) % 4)
        payload = json.loads(base64.urlsafe_b64decode(raw + padding))
        version, stamp, trace_id = payload
        if version != _CURSOR_VERSION:
            raise ValueError(f"unsupported cursor version: {version!r}")
        if not isinstance(trace_id, str) or not trace_id:
            raise ValueError("cursor is missing trace_id")
        return TraceCursor(created_at=datetime.fromisoformat(stamp), trace_id=trace_id)
    except (ValueError, TypeError, KeyError) as exc:
        # 统一成 ValueError：调用方只需要知道"这个游标不能用"。
        raise ValueError(f"invalid trace cursor: {exc}") from exc


def matches(record: TraceRecord, query: TraceQuery) -> bool:
    """内存后端与 SQL 后端共同的判定基准。

    两套后端各写一遍筛选逻辑最容易漂移，因此这里给出"正确答案"的唯一定义，
    SQL 那侧只负责把它翻译成 WHERE，并有跨后端一致性用例盯着。
    """

    if query.task_id is not None and record.task_id != query.task_id:
        return False
    if query.outcome is not None and record.outcome != query.outcome:
        return False
    if query.stop_reason is not None and record.stop_reason != query.stop_reason:
        return False
    if query.risk is not None and record.risk != query.risk:
        return False

    stamp = record.created_at_stamp
    if query.since is not None and stamp < canonical_stamp(query.since):
        return False
    if query.until is not None and stamp >= canonical_stamp(query.until):
        return False

    if query.cursor is not None:
        anchor = query.cursor.stamp
        # 倒序下的"更旧"：时间更早，或同一时刻但 trace_id 更小。
        if not (stamp < anchor or (stamp == anchor and record.trace_id < query.cursor.trace_id)):
            return False
    return True


def sort_key(record: TraceRecord) -> tuple[str, str]:
    """倒序排列用的键。与 SQL 的 `ORDER BY created_at DESC, trace_id DESC` 等价。"""

    return (record.created_at_stamp, record.trace_id)


class InMemoryTraceStore:
    """进程内实现，按容量上限保留最近的记录。

    刻意做成有界的：留痕用于排障，不该成为无限增长的内存占用。
    多实例部署时必须换成共享存储（例如同库的 trace 表）。
    """

    def __init__(self, capacity: int = 500) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self._items: OrderedDict[str, TraceRecord] = OrderedDict()
        self._capacity = capacity
        self._lock = Lock()

    def save(self, record: TraceRecord) -> None:
        with self._lock:
            self._items[record.trace_id] = record
            self._items.move_to_end(record.trace_id)
            while len(self._items) > self._capacity:
                self._items.popitem(last=False)

    def list(self, *, task_id: UUID | None = None, limit: int = 50) -> list[TraceRecord]:
        return self.query(TraceQuery(task_id=task_id, limit=limit)).items

    def query(self, query: TraceQuery) -> TracePage:
        with self._lock:
            records: Sequence[TraceRecord] = list(self._items.values())

        selected = [record for record in records if matches(record, query)]
        # 新的在前：排障时最关心最近发生了什么。
        selected.sort(key=sort_key, reverse=True)

        page = selected[: max(0, query.limit)]
        next_cursor = None
        # 只有当"后面确实还有"时才给游标，否则调用方会为了一个空页多跑一轮。
        has_more = len(selected) > max(0, query.limit)
        if has_more and page:
            last = page[-1]
            next_cursor = encode_cursor(
                TraceCursor(created_at=last.created_at, trace_id=last.trace_id)
            )
        return TracePage(items=page, next_cursor=next_cursor)

    def summary(self, *, task_id: UUID | None = None) -> TraceSummary:
        return self.query_summary(TraceQuery(task_id=task_id))

    def query_summary(self, query: TraceQuery) -> TraceSummary:
        with self._lock:
            records: Sequence[TraceRecord] = list(self._items.values())
        # 摘要是"整个过滤窗口"的结论，与翻到第几页无关：把游标带进来会让同一个
        # 窗口的未确认占比随翻页变化，那是一个没人能解释的数。
        window = replace(query, cursor=None)
        return summarize([record for record in records if matches(record, window)])

    def count(self, query: TraceQuery) -> int:
        with self._lock:
            records: Sequence[TraceRecord] = list(self._items.values())
        window = replace(query, cursor=None)
        return sum(1 for record in records if matches(record, window))

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


def summarize(records: Sequence[TraceRecord]) -> TraceSummary:
    """把一组 trace 聚合成指标。空集合返回全零，避免除零。"""

    total = len(records)
    if total == 0:
        return TraceSummary()

    succeeded = sum(1 for record in records if record.outcome == "succeeded")
    failed = sum(1 for record in records if record.outcome == "failed")
    unconfirmed = total - succeeded - failed

    return TraceSummary(
        total=total,
        succeeded=succeeded,
        failed=failed,
        unconfirmed=unconfirmed,
        unconfirmed_rate=round(unconfirmed / total, 4),
        tool_calls=sum(record.tool_calls for record in records),
        blocked_tool_calls=sum(record.blocked_tool_calls for record in records),
        failed_tool_calls=sum(record.failed_tool_calls for record in records),
        timed_out_tool_calls=sum(record.timed_out_tool_calls for record in records),
        skipped_tool_calls=sum(record.skipped_tool_calls for record in records),
        avg_model_calls=round(sum(record.model_calls for record in records) / total, 2),
        avg_duration_ms=round(sum(record.duration_ms for record in records) / total, 1),
        tokens_total=sum(record.tokens for record in records),
    )
