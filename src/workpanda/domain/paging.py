"""只读检索的公共原语：规范化时间戳、不透明游标、一页结果。

运维只读接口原本只有 trace 一侧支持过滤与键集翻页，其余端点只给 `limit`，
数据一多就静默截断——运维看到的是一份"不完整但看起来完整"的列表。
这里把那一套语义抽成可复用的原语，四个只读端点共用同一份定义：

- 过滤条件之间是**与**，不是或；
- 时间窗左闭右开：`since <= t < until`，相邻窗口拼起来不重不漏；
- 翻页锚在"上一页最后一条"上（键集），不用 offset——有界且持续写入的库上
  offset 会静默漏记录或重复记录；
- 游标带 `scope`：把 `/tasks` 的游标拿去 `/audit` 用必须报错。两个端点各自
  按自己的字段解释同一个字符串，会返回一页"看起来正常"的错误数据，这比
  翻页失效更难发现。

声明在 domain 层是因为游标与查询条件是存储接口的一部分，两套后端都要实现；
它只依赖标准库与 `domain.models`，反向依赖执行层会把分层打穿。
"""

from __future__ import annotations

import base64
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from workpanda.domain.models import EventType, Task, TaskEvent, TaskStatus


def canonical_stamp(value: datetime) -> str:
    """把时间规范成可比较的字符串。

    库里 `created_at` 存的是 ISO 文本，跨后端排序、翻页游标都靠字符串比较。
    只有所有写入与游标都经过同一个规范化函数，比较结果才可靠：

    - 一律换算到 UTC，避免 `+08:00` 与 `+00:00` 混写导致"同一时刻排成两个位置"；
    - 微秒位固定保留，避免 `...:00` 与 `...:00.000000` 长短不一；
    - 不带时区的值按 UTC 处理（写入口只有本模块，正常不会出现）。

    时区混写是真实隐患：早先只写 `datetime.isoformat()`，一旦有人传了带偏移的
    时间，字符串顺序就和真实时间顺序不一致，翻页会漏记录或者重复记录。
    """

    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="microseconds")


_CURSOR_VERSION = 1


def encode_token(scope: str, parts: Sequence[str]) -> str:
    """把锚点编成不透明字符串。

    刻意做成对调用方不透明：游标是内部位置，不是可以手写的查询参数。
    手搓的游标要么被静默当成第一页（用户以为看全了，其实没有），要么行为未定义。
    """

    payload = json.dumps(
        [_CURSOR_VERSION, scope, *parts], separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_token(raw: str, *, scope: str, arity: int) -> list[str]:
    """解析游标。任何不合法都抛 `ValueError`，绝不悄悄退回第一页。"""

    try:
        padding = "=" * (-len(raw) % 4)
        payload = json.loads(base64.urlsafe_b64decode(raw + padding))
        if not isinstance(payload, list) or len(payload) != arity + 2:
            raise ValueError("cursor has an unexpected shape")
        version, cursor_scope, *parts = payload
        if version != _CURSOR_VERSION:
            raise ValueError(f"unsupported cursor version: {version!r}")
        # 跨端点复用游标必须拒绝：同一串字符按另一套字段解释会得到错误的一页，
        # 而那一页的数据看上去完全正常。
        if cursor_scope != scope:
            raise ValueError(f"cursor belongs to {cursor_scope!r}, not {scope!r}")
        if not all(isinstance(part, str) and part for part in parts):
            raise ValueError("cursor is missing its anchor")
        return parts
    except (ValueError, TypeError, KeyError) as exc:
        # 统一成 ValueError：调用方只需要知道"这个游标不能用"。
        raise ValueError(f"invalid {scope} cursor: {exc}") from exc


# ---------------------------------------------------------------------------
# 任务
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TaskCursor:
    """键集翻页的锚点：上一页最后一条任务的 `(created_at, task_id)`。

    用 `(created_at, task_id)` 而不是只用时间：同一毫秒创建的任务在批量
    受理时并不罕见，只有加上唯一的 task_id 才有严格全序，翻页才不会在
    同一时刻上原地打转（要么死循环，要么漏一条）。
    """

    created_at: datetime
    task_id: UUID

    @property
    def stamp(self) -> str:
        return canonical_stamp(self.created_at)


@dataclass(frozen=True, slots=True)
class TaskQuery:
    """一次任务检索的完整条件。过滤之间是"与"。"""

    flow_id: str | None = None
    status: TaskStatus | None = None
    since: datetime | None = None
    until: datetime | None = None
    limit: int = 50
    cursor: TaskCursor | None = None


@dataclass(frozen=True, slots=True)
class TaskPage:
    """一页任务。`next_cursor` 为空表示"到头了"，不是"这一页是空的"。"""

    items: tuple[Task, ...] = ()
    next_cursor: str | None = None


def task_matches(task: Task, query: TaskQuery) -> bool:
    """内存后端与 SQL 后端共同的判定基准。

    两套后端各写一遍筛选逻辑最容易漂移，因此这里给出"正确答案"的唯一定义，
    SQL 那侧只负责把它翻译成 WHERE，并有跨后端一致性用例盯着。
    """

    if query.flow_id is not None and task.flow_id != query.flow_id:
        return False
    if query.status is not None and task.status is not query.status:
        return False

    stamp = canonical_stamp(task.created_at)
    if query.since is not None and stamp < canonical_stamp(query.since):
        return False
    if query.until is not None and stamp >= canonical_stamp(query.until):
        return False

    if query.cursor is not None:
        anchor = query.cursor.stamp
        # 倒序下的"更旧"：时间更早，或同一时刻但 task_id 更小。
        if not (
            stamp < anchor
            or (stamp == anchor and str(task.task_id) < str(query.cursor.task_id))
        ):
            return False
    return True


def task_sort_key(task: Task) -> tuple[str, str]:
    """倒序排列用的键。与 SQL 的 `ORDER BY created_at DESC, task_id DESC` 等价。"""

    return (canonical_stamp(task.created_at), str(task.task_id))


# ---------------------------------------------------------------------------
# 任务事件
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EventQuery:
    """一次事件检索的完整条件。

    事件不需要不透明游标：`sequence` 本身就是任务内唯一、单调的全序，
    翻页锚点直接就是"上一页最后一条的序号"，回传给 `after` 即可。
    给一个整数序号套 base64 只会让调用方多一层猜谜。
    """

    task_id: UUID
    after_sequence: int = 0
    kind: EventType | None = None
    since: datetime | None = None
    until: datetime | None = None
    limit: int = 100


@dataclass(frozen=True, slots=True)
class EventPage:
    """一页事件。`next_cursor` 是下一页的 `after` 值，为空表示到头了。"""

    items: tuple[TaskEvent, ...] = ()
    next_cursor: str | None = None


def event_matches(event: TaskEvent, query: EventQuery) -> bool:
    """事件筛选的唯一判定基准（SQL 侧是它的翻译）。"""

    if event.task_id != query.task_id:
        return False
    if event.sequence <= query.after_sequence:
        return False
    if query.kind is not None and event.type is not query.kind:
        return False

    stamp = canonical_stamp(event.occurred_at)
    if query.since is not None and stamp < canonical_stamp(query.since):
        return False
    if query.until is not None and stamp >= canonical_stamp(query.until):
        return False
    return True
