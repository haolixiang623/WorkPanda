"""运维接口，供 Octop 控制台读取任务与事件。

控制台替代自建运维界面，但不持有业务状态写入权：这里的写入口只有
`POST /tasks/{id}/notes`（追加人工处置备注，不改状态、不推进流程），
流程推进、群众确认和副作用仍只能通过群众侧接口完成。

接口默认关闭；必须配置 `WORKPANDA_OPS_TOKEN` 或 `WORKPANDA_OPS_TOKENS` 才能访问。
可见范围按主体角色收窄：`viewer` 只看列表与聚合指标，明细（任务详情、事件流、
留痕原文、审计归档、工具台账）只有 `admin` 能看。角色划分理由见 `ops_auth`。
"""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status

from workpanda.api.dependencies import get_container
from workpanda.api.ops_auth import OpsDisabled, OpsPrincipal, OpsUnauthenticated
from workpanda.bootstrap import Container
from workpanda.domain.models import (
    EventType,
    TaskEvent,
    TaskNoteRequest,
    TaskStatus,
    TaskView,
)
from workpanda.domain.paging import (
    EventQuery,
    TaskCursor,
    TaskQuery,
    decode_token,
)
from workpanda.harness.audit import AuditCursor, AuditEntry, AuditKind, AuditQuery
from workpanda.harness.trace import (
    TraceCursor,
    TracePage,
    TraceQuery,
    TraceRecord,
    TraceSummary,
    decode_cursor,
)

router = APIRouter(prefix="/api/ops", tags=["ops"])

CURSOR_HEADER = "X-Next-Cursor"
_CURSOR_RESPONSES = {
    200: {
        "headers": {
            CURSOR_HEADER: {
                "description": "下一页游标；为空表示已到末尾。作为下一页的 cursor 参数回传。",
                "schema": {"type": "string"},
            }
        }
    }
}


def _invalid_cursor() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={"code": "invalid_cursor", "detail": "游标无效，请从第一页重新获取。"},
    )


def _parse_cursor(raw: str | None) -> TraceCursor | None:
    """游标错就是 400，不能"忽略它返回第一页"。

    静默退回第一页会让调用方在翻到第三页之后悄悄又拿到第一页，而它只看到
    "有数据"——这是分页最典型的错误表现方式，也是运维最容易误判成"数据重复"的一种。
    """

    if raw is None:
        return None
    try:
        return decode_cursor(raw)
    except ValueError as exc:
        raise _invalid_cursor() from exc


def _parse_task_cursor(raw: str | None) -> TaskCursor | None:
    """任务游标。带 scope 校验：拿 trace 的游标查任务必须 400。"""

    if raw is None:
        return None
    try:
        stamp, task_id = decode_token(raw, scope="tasks", arity=2)
        return TaskCursor(created_at=datetime.fromisoformat(stamp), task_id=UUID(task_id))
    except (ValueError, TypeError) as exc:
        raise _invalid_cursor() from exc


def _parse_audit_cursor(raw: str | None) -> AuditCursor | None:
    """归档游标，同样按 scope 拒绝跨端点复用。"""

    if raw is None:
        return None
    try:
        stamp, entry_id = decode_token(raw, scope="audit", arity=2)
        return AuditCursor(at=datetime.fromisoformat(stamp), entry_id=entry_id)
    except (ValueError, TypeError) as exc:
        raise _invalid_cursor() from exc


def _parse_sequence_cursor(raw: str | None) -> int | None:
    """事件翻页锚点就是序号本身，不做 base64 包装。

    序号已经是任务内唯一、单调的全序，套一层不透明串只会让调用方多一层猜谜；
    非法值仍然报 400，不会静默回到第一条。
    """

    if raw is None:
        return None
    text = raw.strip()
    if not text.isdigit():
        raise _invalid_cursor()
    return int(text)


def _set_cursor(response: Response, cursor: str | None) -> None:
    if cursor is not None:
        response.headers[CURSOR_HEADER] = cursor


def _check_window(since: datetime | None, until: datetime | None) -> None:
    """时间窗必须带时区、且不能反着给。

    不带偏移量的时间会被当成 UTC，对东八区的运维来说是凭空错开 8 小时——错得
    无声无息，查出来的"空结果"看起来就像"那段时间真没出事"。宁可 400 让人补上时区。
    """

    for name, value in (("since", since), ("until", until)):
        if value is not None and value.tzinfo is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "naive_time_window",
                    "detail": f"{name} 必须带时区偏移，例如 2026-11-07T00:00:00+08:00。",
                },
            )
    if since is not None and until is not None and since >= until:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "empty_time_window", "detail": "until 必须晚于 since。"},
        )


OPS_TOKEN_HEADER = "X-WorkPanda-Ops-Token"


def get_ops_principal(
    x_workpanda_ops_token: str | None = Header(default=None),
    container: Container = Depends(get_container),
) -> OpsPrincipal:
    """把运维凭据换成主体。未配置凭据时按不存在处理，不暴露接口存在性。"""

    try:
        return container.ops_access.authenticate(x_workpanda_ops_token)
    except OpsDisabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found") from None
    except OpsUnauthenticated:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "ops_token_invalid", "detail": "运维凭据无效。"},
        ) from None


def require_ops_admin(
    principal: OpsPrincipal = Depends(get_ops_principal),
) -> OpsPrincipal:
    """明细端点只对 admin 开放。

    这里用 403 而不是 404：凭据已经验过，主体确实存在、只是权限不够。运维需要
    区分"我没有这个角色"和"这个接口不存在"，否则会反复怀疑是不是版本没上。
    未授权的第三方仍然拿不到任何信息——它连有效凭据都没有，会停在 401。
    """

    if not principal.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "ops_forbidden",
                "detail": "当前运维角色无权查看明细数据，请使用 admin 角色凭据。",
            },
        )
    return principal


@router.get(
    "/tasks",
    response_model=list[TaskView],
    dependencies=[Depends(get_ops_principal)],
    responses=_CURSOR_RESPONSES,
)
async def list_tasks(
    response: Response,
    limit: int = Query(default=50, ge=1, le=200),
    task_status: TaskStatus | None = Query(default=None, alias="status"),
    flow_id: str | None = Query(default=None, max_length=128),
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
    cursor: str | None = Query(default=None),
    container: Container = Depends(get_container),
) -> list[TaskView]:
    """按创建时间倒序列出任务，条件之间是"与"。

    响应体保持数组（控制台与插件都在按数组解析），翻页信息放在
    `X-Next-Cursor` 头里：把"还有下一页"变成调用方拿得到的事实，而不是
    它从 `len(items) == limit` 猜出来的猜测——猜法在"刚好整页"时必然猜错。
    """

    _check_window(since, until)
    page = container.tasks.query_tasks(
        TaskQuery(
            flow_id=flow_id,
            status=task_status,
            since=since,
            until=until,
            limit=limit,
            cursor=_parse_task_cursor(cursor),
        )
    )
    _set_cursor(response, page.next_cursor)
    return [TaskView.from_task(task) for task in page.items]


@router.get(
    "/tasks/{task_id}", response_model=TaskView, dependencies=[Depends(require_ops_admin)]
)
async def get_task(task_id: UUID, container: Container = Depends(get_container)) -> TaskView:
    return TaskView.from_task(container.tasks.ops_task(task_id))


@router.post(
    "/tasks/{task_id}/notes",
    response_model=TaskEvent,
    status_code=status.HTTP_201_CREATED,
)
async def add_operator_note(
    task_id: UUID,
    request: TaskNoteRequest,
    principal: OpsPrincipal = Depends(require_ops_admin),
    container: Container = Depends(get_container),
) -> TaskEvent:
    """追加一条人工处置备注。**这是整个 `/api/ops` 下唯一的写接口。**

    它只往事件流里追加事实：不改任务状态、不触达执行器、不动确认凭证，也不
    出现在群众侧事件流里。存在的理由很具体——任务落到 `outcome_unconfirmed`
    或 `handoff_required` 之后，运营在系统外核实了结果（对方系统其实办成了、
    群众电话里撤销了、单据号对上了），而这份事实此前只能记在 Excel 里，系统
    里的记录于是长期与现实不符，事后谁也说不清当时核实到了什么。

    署名取自已鉴权的运维主体（`principal.subject`），不接受请求体自报。
    要求 admin 角色：备注会带主体标识长期留在事件流里，viewer 是日常值班角色，
    不该有能力往业务记录里写东西。

    刻意不提供"重试"和"改状态"：
    - 重试可能让一笔已经提交的副作用再发生一次，群众被扣两次钱、收到两份材料；
    - 改状态会把虚假成功制度化，正好是本项目用结构化结果契约定点消灭的缺陷。
    两者都不是这个接口的扩展方向，需要时另做设计评审。
    """

    return container.tasks.add_operator_note(task_id, request, principal.subject)


@router.get(
    "/tasks/{task_id}/events",
    response_model=list[TaskEvent],
    dependencies=[Depends(require_ops_admin)],
    responses=_CURSOR_RESPONSES,
)
async def list_events(
    task_id: UUID,
    response: Response,
    after: int = Query(default=0, ge=0),
    kind: EventType | None = Query(default=None, alias="type"),
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    cursor: str | None = Query(default=None),
    container: Container = Depends(get_container),
) -> list[TaskEvent]:
    """按序号升序列出一个任务的事件。

    `after` 与 `cursor` 是同一件事的两种写法：前者是既有参数（控制台在用），
    后者接收 `X-Next-Cursor`。同时给就 400——两种来源各指一处时，"谁优先"
    会变成靠猜的规则，翻页也就有了两套答案。
    """

    _check_window(since, until)
    if cursor is not None and after:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "invalid_cursor",
                "detail": "after 与 cursor 不能同时使用，请只回传 X-Next-Cursor。",
            },
        )
    anchor = _parse_sequence_cursor(cursor)
    page = container.tasks.query_events(
        EventQuery(
            task_id=task_id,
            after_sequence=anchor if anchor is not None else after,
            kind=kind,
            since=since,
            until=until,
            limit=limit,
        )
    )
    _set_cursor(response, page.next_cursor)
    return list(page.items)


@router.get(
    "/traces/summary",
    response_model=TraceSummary,
    dependencies=[Depends(get_ops_principal)],
)
async def trace_summary(
    task_id: UUID | None = Query(default=None),
    outcome: str | None = Query(default=None, max_length=16),
    stop_reason: str | None = Query(default=None, max_length=32),
    risk: str | None = Query(default=None, max_length=16),
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
    container: Container = Depends(get_container),
) -> TraceSummary:
    """聚合指标：未确认占比是最关键的健康信号，不是任务数量。

    支持与 `/traces` 完全相同的过滤条件——「这段时间的未确认占比是多少」才是
    运维真正要问的问题，只给全局数字等于把窗口叠加的工作丢给调用方自己算，
    而多数人会用「翻几页数一数」去凑，那个数一定是错的。
    """

    _check_window(since, until)
    query = TraceQuery(
        task_id=task_id,
        outcome=outcome,
        stop_reason=stop_reason,
        risk=risk,
        since=since,
        until=until,
    )
    return container.traces.query_summary(query)


@router.get(
    "/traces",
    response_model=list[TraceRecord],
    dependencies=[Depends(require_ops_admin)],
    responses=_CURSOR_RESPONSES,
)
async def list_traces(
    response: Response,
    task_id: UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    outcome: str | None = Query(default=None, max_length=16),
    stop_reason: str | None = Query(default=None, max_length=32),
    risk: str | None = Query(default=None, max_length=16),
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
    cursor: str | None = Query(default=None),
    container: Container = Depends(get_container),
) -> list[TraceRecord]:
    """按条件检索留痕，新的在前。

    响应体保持为数组（控制台已经在按数组解析），翻页信息放在 `X-Next-Cursor`
    响应头里：不变更既有形状，同时把"还有下一页"变成调用方拿得到的事实，而不是
    它从 `len(items) == limit` 猜出来的猜测——猜法在"刚好整页"时必然猜错。
    """

    _check_window(since, until)
    page: TracePage = container.traces.query(
        TraceQuery(
            task_id=task_id,
            outcome=outcome,
            stop_reason=stop_reason,
            risk=risk,
            since=since,
            until=until,
            limit=limit,
            cursor=_parse_cursor(cursor),
        )
    )
    if page.next_cursor is not None:
        response.headers[CURSOR_HEADER] = page.next_cursor
    return page.items


@router.get(
    "/tool-runtime",
    dependencies=[Depends(require_ops_admin)],
)
async def tool_runtime(container: Container = Depends(get_container)) -> dict:
    """工具线程台账。`stuck` 是"已超时、但线程还没退出"的数量。

    超时的工具线程无法被强制终止，只能等它自己结束。这个数接近上限时，
    新的工具调用会被直接拒绝（`tool_capacity`）——那时该处理的是卡住的工具，
    不是继续堆线程。未启用 harness 时没有工具线程，返回 `available=false`。
    """

    stats = container.tool_threads
    if stats is None:
        return {"available": False}
    return {"available": True, **stats()}


@router.get(
    "/audit",
    response_model=list[AuditEntry],
    dependencies=[Depends(require_ops_admin)],
    responses=_CURSOR_RESPONSES,
)
async def list_audit(
    response: Response,
    task_id: UUID | None = Query(default=None),
    kind: AuditKind | None = Query(default=None),
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
    container: Container = Depends(get_container),
) -> list[AuditEntry]:
    """审计归档：只追加、不被容量淘汰，用于核对"某次提交到底发出去没有"。

    未配置归档时返回空列表，而不是 404：接口存在性不随配置变化，
    免得运维看到 404 时误判成"这版还没上这个能力"。
    """

    _check_window(since, until)
    page = container.audit.query(
        AuditQuery(
            task_id=task_id,
            kind=kind,
            since=since,
            until=until,
            limit=limit,
            cursor=_parse_audit_cursor(cursor),
        )
    )
    _set_cursor(response, page.next_cursor)
    return list(page.items)
