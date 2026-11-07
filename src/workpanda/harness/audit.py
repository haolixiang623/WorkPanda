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

import hashlib
import json
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from threading import Lock
from typing import Any, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, ValidationError

from workpanda.domain.paging import canonical_stamp, encode_token
from workpanda.harness.models import utc_now

AUDIT_SCOPE = "audit"


class AuditKind(StrEnum):
    # 一次步骤执行的结论（成/败/未确认），不含模型原文。
    EXECUTION = "execution"
    # 一笔外部动作（已发生 / 结果未知）。
    EFFECT = "effect"


class AuditEntry(BaseModel):
    """一行归档记录。字段刻意保持扁平，便于用 grep / jq 直接查。"""

    # 归档条目的稳定标识：翻页锚点需要严格全序，而 `at` 撞同一微秒、
    # `trace_id` 在一次执行的多条副作用记录间又必然重复，只有它唯一。
    # 为空只出现在旧版本写下的行上，读取时按"文件指纹 + 行号"补一个稳定值。
    entry_id: str = Field(default="", max_length=64)
    at: datetime = Field(default_factory=utc_now)
    kind: AuditKind
    trace_id: str = Field(min_length=1, max_length=64)
    task_id: UUID
    flow_id: str = ""
    flow_version: str = ""
    step_id: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AuditCursor:
    """键集翻页的锚点：上一页最后一条的 `(at, entry_id)`。"""

    at: datetime
    entry_id: str

    @property
    def stamp(self) -> str:
        return canonical_stamp(self.at)


@dataclass(frozen=True, slots=True)
class AuditQuery:
    """一次归档检索的完整条件。过滤之间是"与"。"""

    task_id: UUID | None = None
    kind: AuditKind | None = None
    since: datetime | None = None
    until: datetime | None = None
    limit: int = 50
    cursor: AuditCursor | None = None


@dataclass(frozen=True, slots=True)
class AuditPage:
    """一页归档。`next_cursor` 为空表示"到头了"，不是"这一页是空的"。"""

    items: tuple[AuditEntry, ...] = ()
    next_cursor: str | None = None


def audit_matches(entry: AuditEntry, query: AuditQuery) -> bool:
    """归档筛选的唯一判定基准。"""

    if query.task_id is not None and entry.task_id != query.task_id:
        return False
    if query.kind is not None and entry.kind is not query.kind:
        return False

    stamp = canonical_stamp(entry.at)
    if query.since is not None and stamp < canonical_stamp(query.since):
        return False
    if query.until is not None and stamp >= canonical_stamp(query.until):
        return False

    if query.cursor is not None:
        anchor = query.cursor.stamp
        # 倒序下的"更旧"：时间更早，或同一时刻但 entry_id 更小。
        if not (
            stamp < anchor or (stamp == anchor and entry.entry_id < query.cursor.entry_id)
        ):
            return False
    return True


class AuditJournal(Protocol):
    def append(self, entry: AuditEntry) -> None: ...

    def read(self, *, task_id: UUID | None = None, limit: int = 50) -> list[AuditEntry]: ...

    def query(self, query: AuditQuery) -> AuditPage:
        """带过滤、时间窗与键集翻页的读取。`read` 是稳定的小接口，这是完整版。"""
        ...


class NullAuditJournal:
    """未配置归档时的空实现：调用点不必到处判空。"""

    def append(self, entry: AuditEntry) -> None:
        return

    def read(self, *, task_id: UUID | None = None, limit: int = 50) -> list[AuditEntry]:
        return []

    def query(self, query: AuditQuery) -> AuditPage:
        return AuditPage()


def _fingerprint(lines: Sequence[str]) -> str:
    """文件指纹：取首行的哈希。

    滚动只改名、不改内容，所以同一个文件在 `audit.jsonl` 与 `audit.jsonl.1`
    两种身份下指纹相同——这正是旧记录 id 在滚动之后仍然稳定的前提。
    """

    if not lines:
        return "empty"
    return hashlib.sha1(lines[0].encode("utf-8")).hexdigest()[:8]


def _legacy_entry_id(fingerprint: str, index: int) -> str:
    """给旧版本写下的行补一个稳定 id（文件指纹 + 行内序号）。"""

    return "legacy-" + hashlib.sha1(f"{fingerprint}:{index}".encode()).hexdigest()[:16]


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
        # 编号由归档负责：调用方给不出"跨进程唯一且稳定"的 id，
        # 而翻页锚点没有它就退化成"同一微秒的记录随机排序"。
        if not entry.entry_id:
            entry = entry.model_copy(update={"entry_id": uuid4().hex})
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
        """读归档，新的在前。

        检索范围是当前文件加上全部滚动备份（`.1`、`.2`……由新到旧），
        否则"三个月前那次提交到底发出去没有"这个问题在滚动之后就没人答得了。
        `limit` 是跨文件合计的上限，不是每个文件各算一遍。
        按行容错：一行坏掉不能让整份归档读不出来——这正是审计数据最不该出现的失败方式。
        """

        limit = max(0, limit)
        if limit == 0:
            return []

        return self._scan(
            predicate=lambda entry: task_id is None or entry.task_id == task_id,
            limit=limit,
        )

    def query(self, query: AuditQuery) -> AuditPage:
        """带过滤、时间窗与键集翻页的读取。

        顺序按 `(at, entry_id)` 倒序：文件顺序就是追加顺序，正常情况下两者一致，
        但并发写入可能让 `at` 与落盘顺序轻微错位。翻页只认这个全序，才不会因为
        "文件顺序与时间顺序不一致"而漏记录。
        """

        limit = max(0, query.limit)
        selected = [entry for entry in self._scan() if audit_matches(entry, query)]
        selected.sort(key=lambda entry: (canonical_stamp(entry.at), entry.entry_id), reverse=True)

        page = selected[:limit]
        next_cursor = None
        # 只有当"后面确实还有"时才给游标，否则调用方会为了一个空页多跑一轮。
        if len(selected) > limit and page:
            last = page[-1]
            next_cursor = encode_token(AUDIT_SCOPE, [canonical_stamp(last.at), last.entry_id])
        return AuditPage(items=tuple(page), next_cursor=next_cursor)

    def _scan(
        self,
        *,
        predicate: Callable[[AuditEntry], bool] | None = None,
        limit: int | None = None,
    ) -> list[AuditEntry]:
        """按"当前文件 + 备份"由新到旧读出条目，坏行跳过。

        旧版本写下的行没有 `entry_id`，这里按"文件指纹 + 行号"补一个稳定值：
        文件指纹取首行的哈希（滚动只改名不改内容，所以改名之后仍成立），
        行号在文件内唯一，因此同一行每次读到的 id 一致，翻页锚点不会漂移。

        `limit` 只给"只要最近 N 条"的调用方用（`read`）：文件顺序就是时间顺序，
        够数就停，不必把整份归档读进内存。按时间排序的检索（`query`）必须全扫，
        因为同一个 `(at, entry_id)` 全序不能靠"读到够为止"保证。
        """

        entries: list[AuditEntry] = []
        # 读也在锁内：滚动是先改名再重开，放开锁就可能读到"当前文件刚被搬走"的空档。
        with self._lock:
            for path in self._paths_newest_first():
                if not path.exists():
                    continue
                lines = [
                    line
                    for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
                    if line.strip()
                ]
                fingerprint = _fingerprint(lines)
                # 文件内倒序 = 由新到旧；跨文件按 `_paths_newest_first()` 的顺序。
                for index in range(len(lines) - 1, -1, -1):
                    try:
                        entry = AuditEntry.model_validate_json(lines[index])
                    except (ValidationError, ValueError):
                        continue
                    if not entry.entry_id:
                        entry = entry.model_copy(
                            update={"entry_id": _legacy_entry_id(fingerprint, index)}
                        )
                    if predicate is not None and not predicate(entry):
                        continue
                    entries.append(entry)
                    if limit is not None and len(entries) >= limit:
                        return entries
        return entries

    def _paths_newest_first(self) -> list[Path]:
        """当前文件在前，其后依次是 `.1`、`.2`……由新到旧。"""
        return [self._path, *(self.backup_path(index) for index in range(1, self._backups + 1))]

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
