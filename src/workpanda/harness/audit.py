"""审计归档：把"发生过什么"写进一个只追加、不会被淘汰的文件。

与 trace 的分工必须说清楚，否则迟早会把两者混成一个东西：

| | trace | 审计归档 |
|---|---|---|
| 形态 | 有界（默认 500 条），旧记录淘汰 | 只追加，按大小滚动 |
| 用途 | 排障、聚合指标 | 回答"三个月前那次提交到底发出去没有" |
| 能否当证据 | 不能，会被覆盖 | 可以，写到盘上就不再改动 |

为什么需要它：一旦开启"非幂等工具"（提交申请、发出材料），事故复盘要核对的
是外部系统的真实状态，而这件事只有一个信息源——当时到底调了哪些工具、成功还是
结果未知。trace 会淘汰，任务事件会随任务归档，都不能长期承担这个职责。

三条不变式：

1. **只追加**：归档文件只写不改，滚动时改名而不是重写，避免"为了压缩而丢证据"；
2. **不落原始参数**：条目只带工具名与参数指纹，业务数据留在任务事件里；
3. **写不进去也不影响业务**：调用方吞掉异常——留痕失败不该把已经完成的事变成失败。
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from threading import Lock
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel, Field, ValidationError

from workpanda.harness.models import utc_now


class AuditKind(StrEnum):
    # 一次步骤执行的结论（成/败/未确认），不含模型原文。
    EXECUTION = "execution"
    # 一笔外部动作（已发生 / 结果未知）。
    EFFECT = "effect"


class AuditEntry(BaseModel):
    """一行归档记录。字段刻意保持扁平，便于用 grep / jq 直接查。"""

    at: datetime = Field(default_factory=utc_now)
    kind: AuditKind
    trace_id: str = Field(min_length=1, max_length=64)
    task_id: UUID
    flow_id: str = ""
    flow_version: str = ""
    step_id: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)


class AuditJournal(Protocol):
    def append(self, entry: AuditEntry) -> None: ...

    def read(self, *, task_id: UUID | None = None, limit: int = 50) -> list[AuditEntry]: ...


class NullAuditJournal:
    """未配置归档时的空实现：调用点不必到处判空。"""

    def append(self, entry: AuditEntry) -> None:
        return

    def read(self, *, task_id: UUID | None = None, limit: int = 50) -> list[AuditEntry]:
        return []


class JsonlAuditJournal:
    """追加式 JSONL 归档。

    每次写入都 `fsync`：归档的价值全在"进程被 kill 之后还查得到"，
    省掉这一步等于把证据留在页缓存里。写入频率是"每个步骤一次"级别，代价可接受。

    滚动策略：当前文件将达到 `max_bytes` 时，`audit.jsonl` → `audit.jsonl.1`，
    旧的 `.1` → `.2`，最多保留 `backups` 份，更旧的直接删除。
    `backups=0` 表示不保留备份（只留当前文件）。
    """

    def __init__(
        self,
        path: Path | str,
        *,
        max_bytes: int = 5_000_000,
        backups: int = 3,
        fsync: bool = True,
    ) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be >= 1")
        if backups < 0:
            raise ValueError("backups must be >= 0")
        self._path = Path(path)
        self._max_bytes = max_bytes
        self._backups = backups
        self._fsync = fsync
        self._lock = Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def append(self, entry: AuditEntry) -> None:
        line = json.dumps(entry.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
        data = (line + "\n").encode("utf-8")
        with self._lock:
            self._rotate_if_needed(len(data))
            with self._path.open("ab") as handle:
                handle.write(data)
                handle.flush()
                if self._fsync:
                    os.fsync(handle.fileno())

    def read(self, *, task_id: UUID | None = None, limit: int = 50) -> list[AuditEntry]:
        """读当前归档文件，新的在前。

        只读当前文件（滚动出去的备份需人工/离线查），并按行容错：
        一行坏掉不能让整份归档读不出来——这正是审计数据最不该出现的失败方式。
        """

        limit = max(0, limit)
        if limit == 0 or not self._path.exists():
            return []

        with self._lock:
            text = self._path.read_text(encoding="utf-8", errors="replace")

        entries: list[AuditEntry] = []
        for line in reversed(text.splitlines()):
            if not line.strip():
                continue
            try:
                entry = AuditEntry.model_validate_json(line)
            except (ValidationError, ValueError):
                continue
            if task_id is not None and entry.task_id != task_id:
                continue
            entries.append(entry)
            if len(entries) >= limit:
                break
        return entries

    def backup_path(self, index: int) -> Path:
        return Path(f"{self._path}.{index}")

    def _rotate_if_needed(self, incoming: int) -> None:
        current = self._path.stat().st_size if self._path.exists() else 0
        if current == 0 or current + incoming <= self._max_bytes:
            return
        if self._backups == 0:
            self._path.unlink(missing_ok=True)
            return
        self.backup_path(self._backups).unlink(missing_ok=True)
        for index in range(self._backups - 1, 0, -1):
            source = self.backup_path(index)
            if source.exists():
                source.replace(self.backup_path(index + 1))
        self._path.replace(self.backup_path(1))


def entries_for_records(
    *,
    trace_id: str,
    task_id: UUID,
    flow_id: str,
    flow_version: str,
    step_id: str,
    execution: dict[str, Any],
    effects: Sequence[Any],
) -> list[AuditEntry]:
    """把一次执行拼成"一条执行记录 + N 条副作用记录"。

    拼装放在这里而不是执行器里：归档的字段口径只应有一处定义，
    否则运维查两条记录时会发现同名指标对不上。
    """

    entries = [
        AuditEntry(
            kind=AuditKind.EXECUTION,
            trace_id=trace_id,
            task_id=task_id,
            flow_id=flow_id,
            flow_version=flow_version,
            step_id=step_id,
            payload=dict(execution),
        )
    ]
    for effect in effects:
        payload = effect.model_dump(mode="json")
        for key in ("trace_id", "task_id", "flow_id", "flow_version", "step_id"):
            payload.pop(key, None)
        entries.append(
            AuditEntry(
                kind=AuditKind.EFFECT,
                trace_id=trace_id,
                task_id=task_id,
                flow_id=flow_id,
                flow_version=flow_version,
                step_id=step_id,
                payload=payload,
            )
        )
    return entries
