"""执行留痕的独立存储与聚合。

此前 trace 只随任务结果一起存进 `Task.result`，后果是：

- 只能按任务查，无法回答"最近哪些步骤在超预算""哪类工具老被拦"；
- 成功执行的 trace 没有事件承载点，等于不落盘；
- 想加指标就得把全部任务翻一遍。

这里把"一次执行"抽成一条 `TraceRecord`，单独存储并聚合，业务任务状态仍以
Task Store 为准——trace 只描述"执行发生了什么"，不参与流程推进。
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Sequence
from datetime import UTC, datetime
from threading import Lock
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, Field


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
    def save(self, record: TraceRecord) -> None: ...

    def list(self, *, task_id: UUID | None = None, limit: int = 50) -> list[TraceRecord]: ...

    def summary(self, *, task_id: UUID | None = None) -> TraceSummary: ...


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
        with self._lock:
            records: Sequence[TraceRecord] = list(self._items.values())
        if task_id is not None:
            records = [record for record in records if record.task_id == task_id]
        # 新的在前：排障时最关心最近发生了什么。
        return list(reversed(records))[: max(0, limit)]

    def summary(self, *, task_id: UUID | None = None) -> TraceSummary:
        records = self.list(task_id=task_id, limit=self._capacity)
        return summarize(records)

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
