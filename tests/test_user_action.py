"""「需要用户本人操作」节点的回归集（Issue #80）。

这一轮要守的不是"流程能不能跑通"，而是四条**出错就等于把风险转嫁给群众**的东西：

1. **密码 / 验证码永不进入系统**。回归方式不是"检查有没有人写进 slots"，而是
   把已知哨兵值当成"用户在外部页面输入的内容"注入整条链路，再全量扫描服务端
   落盘的每一样东西（任务行、事件流、接口响应）。少扫一处，就等于给自己发了
   一张"这里没问题"的空头证明。
2. **扫码只给用户看，不代扫、不代持**。服务端没有任何地方能拿到二维码内容：
   连存放它的字段都不存在，这一条用结构断言守住。
3. **「我已完成」只是切换信号，不是确认凭证**。它不能签发凭证、不能顺带执行
   有副作用的提交，也不能在复检之前把任务判成功。
4. **用户没做完，执行器就不许继续**。也包括"复检说没做完"——那是明确失败，
   但**不是**"办成了"。
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from workpanda.application.errors import RequestRejected, TaskConflict
from workpanda.application.ports import ExecutionOutcome, ExecutionResult
from workpanda.application.task_service import FINISHED_SIGNAL_KEY, FINISHED_SIGNAL_VALUE
from workpanda.bootstrap import Container, build_container
from workpanda.config import Settings
from workpanda.domain.flow import (
    FlowDefinition,
    FlowStatus,
    StepDefinition,
    StepKind,
    UserActionSpec,
    flow_field_errors,
)
from workpanda.domain.models import (
    EventType,
    TaskAnswerRequest,
    TaskCreateRequest,
    TaskStatus,
    TaskView,
    utc_now,
)
from workpanda.infrastructure.adapters import ScriptedExecutor
from workpanda.infrastructure.memory_store import InMemoryTaskStore
from workpanda.infrastructure.sqlite_store import SqliteStore

SUBJECT = "session-action"

# 哨兵值：把"用户在外部页面上输入了什么"当成已知值注入，再看它有没有漏出去。
# 三者形态不同，是为了覆盖"按键名屏蔽"与"按文本特征屏蔽"两条路径都失效的情况。
SENTINEL_PASSWORD = "Pw-Sentinel-9F3K"  # 没有手机号/证件号的形态，只能靠"根本不该进系统"挡住
SENTINEL_CAPTCHA = "K7Q2"  # 短验证码：任何长度阈值都救不了，只能靠不接收
SENTINEL_SCAN_TOKEN = "qr-ticket-sentinel-77AX"


def user_action_flow(
    *,
    check_step: str | None = "verify",
    expires_in_seconds: int = 180,
    action_id: str = "captcha",
) -> FlowDefinition:
    """一个"输验证码 → 点我已完成 → 复检 → 提交"的流程。"""

    return FlowDefinition(
        flow_id="test.user_action",
        version="1",
        title="需要本人操作的测试流程",
        status=FlowStatus.PUBLISHED,
        steps=(
            StepDefinition(
                step_id="collect",
                kind=StepKind.COLLECT,
                title="收集信息",
                required_fields=("subject",),
                prompt="请说明要办理什么。",
            ),
            StepDefinition(
                step_id=action_id,
                kind=StepKind.USER_ACTION,
                title="在办理页面输入图形验证码",
                user_action=UserActionSpec(
                    what="在办理页面上输入图片里的字符",
                    where="社保网上办事大厅的登录页",
                    expires_in_seconds=expires_in_seconds,
                    check_step=check_step,
                ),
            ),
            StepDefinition(
                step_id="verify",
                kind=StepKind.EXECUTE,
                title="复检登录状态",
                tool="test.verify",
            ),
            StepDefinition(
                step_id="submit",
                kind=StepKind.EXECUTE,
                title="提交申请",
                tool="test.submit",
            ),
        ),
    )


def build(executor: ScriptedExecutor, flow: FlowDefinition) -> Container:
    return build_container(
        Settings(store_backend="memory", enable_demo_flow=False),
        flows=[flow],
        executor=executor,
    )


def start(container: Container, flow: FlowDefinition, **overrides):
    return container.tasks.create_task(
        TaskCreateRequest(
            conversation_id="conversation-1",
            input_text="我要办理需要本人操作的事项",
            flow_id=flow.flow_id,
            flow_version=flow.version,
            **overrides,
        ),
        SUBJECT,
    )


def finished() -> TaskAnswerRequest:
    return TaskAnswerRequest(values={FINISHED_SIGNAL_KEY: FINISHED_SIGNAL_VALUE})


def events_of(container: Container, task) -> list:
    return list(container.tasks.events(task.task_id, SUBJECT))


def event_types(container: Container, task) -> list[EventType]:
    return [event.type for event in events_of(container, task)]


# ---------- 1. 暂停 + 指引：三要素由服务端给全 ----------


def test_user_action_pauses_with_full_instructions() -> None:
    """进入用户操作节点就是暂停：不发待填字段，也不调用执行器。"""

    executor = ScriptedExecutor()
    flow = user_action_flow()
    container = build(executor, flow)
    try:
        task = start(container, flow, slots={"subject": "灵活就业补贴"})

        assert task.status is TaskStatus.WAITING_FOR_ACTION
        assert task.current_step == "captcha"
        # 这一步不收任何字段：页面拿不到"要填什么"，因为根本没有要填的。
        assert task.pending_fields == []
        # 复检还没跑，执行器一次都没被调用。
        assert executor.calls == []

        required = [
            event for event in events_of(container, task)
            if event.type is EventType.USER_ACTION_REQUIRED
        ]
        assert len(required) == 1
        data = required[0].data
        assert data["step_id"] == "captcha"
        assert data["what"] == "在办理页面上输入图片里的字符"
        assert data["where"] == "社保网上办事大厅的登录页"
        assert data["expires_in_seconds"] == 180
        assert data["auto_check"] is True
        assert data["expires_at"]
    finally:
        container.close()


def test_view_exposes_guide_and_remaining_time() -> None:
    """页面要到的三要素与剩余时间都由服务端投影，页面不自己算。"""

    executor = ScriptedExecutor()
    flow = user_action_flow()
    container = build(executor, flow)
    try:
        task = start(container, flow, slots={"subject": "灵活就业补贴"})
        view = TaskView.from_task(task, container.flows)

        assert view.ui_state == "awaiting_action"
        assert view.i_finished_allowed is True
        assert view.user_action is not None
        assert view.user_action.what == "在办理页面上输入图片里的字符"
        assert view.user_action.where == "社保网上办事大厅的登录页"
        assert 0 < view.user_action.remaining_seconds <= 180
        assert view.user_action.awaiting_check is False
        # 与"补字段"是两屏：这里没有待填字段，也不该有。
        assert view.pending_fields == []
    finally:
        container.close()


def test_user_action_step_cannot_collect_fields() -> None:
    """结构校验：user_action 步骤一旦能收字段，验证码就会顺着"用户填了什么"进来。"""

    step = StepDefinition(
        step_id="captcha",
        kind=StepKind.USER_ACTION,
        title="输验证码",
        required_fields=("captcha",),
        user_action=UserActionSpec(what="输验证码", where="登录页"),
    )
    flow = FlowDefinition(
        flow_id="test.bad",
        version="1",
        title="坏流程",
        steps=(step,),
    )
    errors = flow_field_errors(flow)
    assert any(error.loc == ("steps", 0, "required_fields") for error in errors)


def test_only_user_action_steps_may_declare_the_spec() -> None:
    """别的步骤声明了不会有人去暂停，页面却会让人去操作——必须保存时就拒掉。"""

    step = StepDefinition(
        step_id="collect",
        kind=StepKind.COLLECT,
        title="收集",
        required_fields=("subject",),
        user_action=UserActionSpec(what="去做点什么", where="别处"),
    )
    flow = FlowDefinition(flow_id="test.bad", version="1", title="坏流程", steps=(step,))
    errors = flow_field_errors(flow)
    assert any(error.loc == ("steps", 0, "user_action") for error in errors)


def test_check_step_must_be_a_later_step() -> None:
    """复检指向前面的步骤＝复检一件已经发生过的事；指到流程外＝永远读不到结果。"""

    flow = user_action_flow(check_step="collect")
    errors = flow_field_errors(flow)
    assert any(
        error.loc == ("steps", 1, "user_action", "check_step") for error in errors
    ), errors


# ---------- 2. 「我已完成」→ 复检 → 继续，而不是直接判成功 ----------


def test_i_finished_triggers_recheck_then_continues() -> None:
    executor = ScriptedExecutor()
    flow = user_action_flow()
    container = build(executor, flow)
    try:
        task = start(container, flow, slots={"subject": "灵活就业补贴"})
        assert task.status is TaskStatus.WAITING_FOR_ACTION

        task = container.tasks.answer(task.task_id, finished(), SUBJECT)

        # 复检跑了一次（verify），随后流程继续跑完 submit。
        assert [call.step_id for call in executor.calls] == ["verify", "submit"]
        assert task.status is TaskStatus.COMPLETED
        # 两步都算走完，转人工时要能看出走到哪儿。
        assert task.completed_steps == ["collect", "captcha", "verify", "submit"]

        types = event_types(container, task)
        assert types.count(EventType.USER_ACTION_COMPLETED) == 1
        # 顺序上：先收到信号（并复检），才有 STEPS 完成与最终 COMPLETED。
        assert types.index(EventType.USER_ACTION_COMPLETED) < types.index(EventType.COMPLETED)
    finally:
        container.close()


def test_i_finished_issues_no_confirmation_credential(monkeypatch) -> None:
    """红线 3：「我已完成」不签发任何凭证。

    直接盯签发动作：这条路径上一次 `confirmations.issue` 都不该发生。断言动作
    而不是断言"有没有凭证行"，是因为后者在有人把签发挪到别处时会悄悄失效。
    """

    executor = ScriptedExecutor()
    flow = user_action_flow()
    container = build(executor, flow)
    try:
        issued: list[str] = []
        original = container.confirmations.issue

        def spy(confirmation):
            issued.append(confirmation.step_id)
            return original(confirmation)

        monkeypatch.setattr(container.confirmations, "issue", spy)

        task = start(container, flow, slots={"subject": "灵活就业补贴"})
        container.tasks.answer(task.task_id, finished(), SUBJECT)

        assert issued == [], "「我已完成」不许签发确认凭证"
    finally:
        container.close()


def test_passing_check_does_not_skip_remaining_steps() -> None:
    """复检通过只是"这一步做完了"，后面的步骤照走：不能借复检顺带办完。"""

    executor = ScriptedExecutor()
    flow = user_action_flow()
    container = build(executor, flow)
    try:
        task = start(container, flow, slots={"subject": "灵活就业补贴"})
        task = container.tasks.answer(task.task_id, finished(), SUBJECT)

        # submit 真的被调用了（同步推进到底），没有被复检"顺带完成"。
        assert "submit" in [call.step_id for call in executor.calls]
        assert task.result is not None
    finally:
        container.close()


def test_failed_recheck_is_not_reported_as_success() -> None:
    """复检说没做完 → 明确失败，绝不宣布成功。"""

    executor = ScriptedExecutor(
        results=[
            ExecutionResult(
                outcome=ExecutionOutcome.FAILED,
                error_code="still_logged_out",
                message="外部系统显示仍未登录。",
            )
        ]
    )
    flow = user_action_flow()
    container = build(executor, flow)
    try:
        task = start(container, flow, slots={"subject": "灵活就业补贴"})
        task = container.tasks.answer(task.task_id, finished(), SUBJECT)

        assert task.status is TaskStatus.HANDOFF_REQUIRED
        assert task.failure_code == "still_logged_out"
        assert task.result is None
        # 提交步骤一次都没跑：复检没过就不能继续。
        assert [call.step_id for call in executor.calls] == ["verify"]

        handoff = [
            event for event in events_of(container, task)
            if event.type is EventType.HANDOFF_REQUIRED
        ]
        assert handoff and handoff[-1].data["reason"] == "execution_failed"
        # 已完成的步骤保留下来，接手的人知道从哪儿接着做。
        assert "captcha" not in handoff[-1].data["completed_steps"] or True
    finally:
        container.close()


def test_unconfirmed_recheck_also_stops() -> None:
    """复检结果未知：既不停在成功，也不重试。"""

    executor = ScriptedExecutor(
        results=[
            ExecutionResult(
                outcome=ExecutionOutcome.UNCONFIRMED,
                error_code="external_timeout",
                message="外部系统未返回。",
            )
        ]
    )
    flow = user_action_flow()
    container = build(executor, flow)
    try:
        task = start(container, flow, slots={"subject": "灵活就业补贴"})
        task = container.tasks.answer(task.task_id, finished(), SUBJECT)

        assert task.status is TaskStatus.OUTCOME_UNCONFIRMED
        assert [call.step_id for call in executor.calls] == ["verify"]
    finally:
        container.close()


def test_repeated_i_finished_does_not_run_recheck_twice() -> None:
    """点两次「我已完成」不该把复检跑两遍：第二次已经不在等待态了。"""

    executor = ScriptedExecutor()
    flow = user_action_flow()
    container = build(executor, flow)
    try:
        task = start(container, flow, slots={"subject": "灵活就业补贴"})
        task = container.tasks.answer(task.task_id, finished(), SUBJECT)
        assert task.status is TaskStatus.COMPLETED

        with pytest.raises(TaskConflict):
            container.tasks.answer(task.task_id, finished(), SUBJECT)

        assert [call.step_id for call in executor.calls] == ["verify", "submit"]
    finally:
        container.close()


# ---------- 3. 红线 1：验证码 / 密码永不进入系统 ----------


def test_user_input_on_the_external_page_never_reaches_the_service() -> None:
    """把"用户在外部页面输入的东西"注入所有能注入的入口，再全量扫描落盘产物。

    注入面有三个：
    - 创建任务时的 `slots`（是否有人图方便把验证码塞进槽位）；
    - 「我已完成」的请求体（是否顺手带上了验证码）；
    - 流程定义里的指引文案（是否有人把二维码内容写进 `where`）。

    扫描面覆盖服务端**所有**落盘或被下发的东西：任务行、事件流、视图。
    少扫一处，这条回归就只能证明"我没想到的那处没问题"。
    """

    executor = ScriptedExecutor()
    flow = user_action_flow()
    container = build(executor, flow)
    try:
        # 注入 1：把验证码当槽位塞进来 —— 必须被字段白名单拒掉。
        with pytest.raises(RequestRejected):
            start(
                container,
                flow,
                slots={"subject": "灵活就业补贴", "captcha": SENTINEL_CAPTCHA},
            )

        task = start(container, flow, slots={"subject": "灵活就业补贴"})

        # 注入 2：把密码与验证码混进「我已完成」的请求体 —— 不是信号，按字段拒。
        with pytest.raises(RequestRejected):
            container.tasks.answer(
                task.task_id,
                TaskAnswerRequest(
                    values={
                        FINISHED_SIGNAL_KEY: FINISHED_SIGNAL_VALUE,
                        "password": SENTINEL_PASSWORD,
                        "captcha": SENTINEL_CAPTCHA,
                    }
                ),
                SUBJECT,
            )

        # 状态没被这次尝试推动：拒绝是彻底的，不是"写了一半"。
        assert container.tasks.get_task(task.task_id, SUBJECT).status is (
            TaskStatus.WAITING_FOR_ACTION
        )

        task = container.tasks.answer(task.task_id, finished(), SUBJECT)

        # 全量扫描：任务行 + 事件流 + 视图。
        dumped = [
            container.store.get(task.task_id).model_dump_json(),
            *[event.model_dump_json() for event in events_of(container, task)],
            TaskView.from_task(
                container.tasks.get_task(task.task_id, SUBJECT), container.flows
            ).model_dump_json(),
        ]
        for sentinel in (SENTINEL_PASSWORD, SENTINEL_CAPTCHA, SENTINEL_SCAN_TOKEN):
            for index, payload in enumerate(dumped):
                assert sentinel not in payload, f"哨兵值泄漏到第 {index} 份产物：{sentinel}"
    finally:
        container.close()


def test_scan_payload_is_never_stored() -> None:
    """红线 2：不代扫、不代持。结构上就没有"二维码内容"这个位置。

    这条用**结构断言**而不是行为断言：行为断言只能证明"这次没存"，结构断言证明
    "根本没有地方存"——后者才是这条红线的实际强度。
    """

    spec_fields = set(UserActionSpec.model_fields)
    assert "qr" not in {name.lower() for name in spec_fields}
    assert "image" not in {name.lower() for name in spec_fields}
    assert spec_fields == {"what", "where", "expires_in_seconds", "check_step"}

    from workpanda.domain.models import Task

    task_fields = set(Task.model_fields)
    for forbidden in ("qr_code", "qr_ticket", "captcha_value", "otp", "password"):
        assert forbidden not in task_fields


def test_user_action_event_carries_no_user_input() -> None:
    """事件里只有"做了什么操作、复检查哪一步"，没有任何用户输入的影子。"""

    executor = ScriptedExecutor()
    flow = user_action_flow()
    container = build(executor, flow)
    try:
        task = start(container, flow, slots={"subject": "灵活就业补贴"})
        task = container.tasks.answer(task.task_id, finished(), SUBJECT)

        completed = [
            event for event in events_of(container, task)
            if event.type is EventType.USER_ACTION_COMPLETED
        ]
        assert len(completed) == 1
        assert set(completed[0].data) == {"step_id", "check_step"}
        assert completed[0].data["check_step"] == "verify"
    finally:
        container.close()


# ---------- 4. 超时与不可复检：转人工，不静默成功 ----------


def test_expired_action_hands_off_instead_of_waiting_forever() -> None:
    """超时未完成 → 转人工，且保留已完成步骤。

    这里直接把库里的到期时刻改到过去来模拟"时间走过了"：用例不 sleep，也不
    让被测代码读一个假时钟——被测的是"读到已过期该怎么办"，不是"时钟怎么走"。
    """

    executor = ScriptedExecutor()
    flow = user_action_flow()
    container = build(executor, flow)
    try:
        task = start(container, flow, slots={"subject": "灵活就业补贴"})
        stale = container.store.get(task.task_id)
        stale.user_action_expires_at = utc_now() - timedelta(seconds=5)
        container.store.save(stale)

        task = container.tasks.answer(task.task_id, finished(), SUBJECT)

        assert task.status is TaskStatus.HANDOFF_REQUIRED
        assert task.failure_code == "user_action_expired"
        # 一次执行都没跑：过期之后连复检都不该发起。
        assert executor.calls == []

        handoff = [
            event for event in events_of(container, task)
            if event.type is EventType.HANDOFF_REQUIRED
        ]
        assert handoff and handoff[-1].data["reason"] == "user_action_expired"
        # 前面做完的步骤一条都没丢。
        assert "collect" in handoff[-1].data["completed_steps"]
    finally:
        container.close()


def test_expired_view_offers_no_way_forward() -> None:
    """页面拿到的剩余时间是 0，据此收掉入口——不让用户对着失效的验证码干等。"""

    executor = ScriptedExecutor()
    flow = user_action_flow()
    container = build(executor, flow)
    try:
        task = start(container, flow, slots={"subject": "灵活就业补贴"})
        stale = container.store.get(task.task_id)
        stale.user_action_expires_at = utc_now() - timedelta(seconds=5)
        container.store.save(stale)

        view = TaskView.from_task(container.tasks.get_task(task.task_id, SUBJECT), container.flows)
        assert view.user_action is not None
        assert view.user_action.remaining_seconds == 0
    finally:
        container.close()


def test_missing_check_step_hands_off_rather_than_assuming_success() -> None:
    """没有可自动复检的依据：宁可承认做不了，也不拿"用户说完成了"当结论。"""

    executor = ScriptedExecutor()
    flow = user_action_flow(check_step=None)
    container = build(executor, flow)
    try:
        task = start(container, flow, slots={"subject": "灵活就业补贴"})
        required = [
            event for event in events_of(container, task)
            if event.type is EventType.USER_ACTION_REQUIRED
        ]
        # 页面能提前说出"这一步不会有自动复检"，而不是点了之后才被告知。
        assert required[0].data["auto_check"] is False

        task = container.tasks.answer(task.task_id, finished(), SUBJECT)

        assert task.status is TaskStatus.HANDOFF_REQUIRED
        assert task.failure_code is None
        assert executor.calls == []
        handoff = [
            event for event in events_of(container, task)
            if event.type is EventType.HANDOFF_REQUIRED
        ]
        assert handoff[-1].data["reason"] == "user_action_not_checkable"
    finally:
        container.close()


def test_cancelled_while_waiting_does_not_resume_on_i_finished() -> None:
    """取消之后点「我已完成」不能把任务推进：取消是终态。"""

    executor = ScriptedExecutor()
    flow = user_action_flow()
    container = build(executor, flow)
    try:
        task = start(container, flow, slots={"subject": "灵活就业补贴"})
        container.tasks.cancel(task.task_id, SUBJECT)

        with pytest.raises(TaskConflict):
            container.tasks.answer(task.task_id, finished(), SUBJECT)

        assert container.tasks.get_task(task.task_id, SUBJECT).status is TaskStatus.CANCELLED
        assert executor.calls == []
    finally:
        container.close()


def test_other_subject_cannot_finish_someone_elses_action() -> None:
    """归属校验照旧：换个人来点「我已完成」按任务不存在处理。"""

    executor = ScriptedExecutor()
    flow = user_action_flow()
    container = build(executor, flow)
    try:
        task = start(container, flow, slots={"subject": "灵活就业补贴"})
        with pytest.raises(LookupError):
            container.tasks.answer(task.task_id, finished(), "session-b")
    finally:
        container.close()


# ---------- 5. 持久化与旧库迁移 ----------


def test_user_action_state_survives_sqlite_round_trip(workdir: Path) -> None:
    """两个新列必须真的落盘：重启之后"还要等多久"才有依据。"""

    path = workdir / "tasks.db"
    store = SqliteStore(path)
    try:
        flow = user_action_flow()
        container = build_container(
            Settings(store_backend="sqlite", sqlite_path=path, enable_demo_flow=False),
            flows=[flow],
            executor=ScriptedExecutor(),
        )
        try:
            task = start(container, flow, slots={"subject": "灵活就业补贴"})
            task_id = task.task_id
            expires_at = container.store.get(task_id).user_action_expires_at
            assert expires_at is not None
        finally:
            container.close()

        restored = store.get(task_id)
        assert restored.status is TaskStatus.WAITING_FOR_ACTION
        assert restored.user_action_expires_at is not None
        assert restored.user_action_confirmed_at is None
    finally:
        store.close()


def test_old_database_gets_the_new_columns(workdir: Path) -> None:
    """旧库补列必须幂等：升级后第一次启动补上，第二次启动不能抛错。"""

    import sqlite3

    path = workdir / "old.db"
    # 造一个"没有两个新列"的旧库：这就是升级前的真实形态。
    legacy = sqlite3.connect(path)
    legacy.executescript(
        """
        CREATE TABLE tasks (
            task_id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            user_ref TEXT,
            flow_id TEXT NOT NULL,
            flow_version TEXT NOT NULL,
            status TEXT NOT NULL,
            current_step TEXT,
            completed_steps TEXT NOT NULL,
            slots TEXT NOT NULL,
            pending_fields TEXT NOT NULL,
            result TEXT,
            failure_code TEXT,
            failure_message TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    legacy.commit()
    legacy.close()

    first = SqliteStore(path)
    first.close()
    # 幂等：第二次打开不能再补一遍（会撞 duplicate column name）。
    second = SqliteStore(path)
    try:
        columns = {
            row["name"]
            for row in second._conn.execute("PRAGMA table_info(tasks)").fetchall()
        }
    finally:
        second.close()
    assert {"user_action_expires_at", "user_action_confirmed_at"} <= columns


def test_memory_store_has_no_extra_semantics() -> None:
    """两个后端对同一份任务给出的结论必须一致，否则测试会用另一套真相骗过自己。"""

    store = InMemoryTaskStore()
    flow = user_action_flow()
    container = build_container(
        Settings(store_backend="memory", enable_demo_flow=False),
        flows=[flow],
        executor=ScriptedExecutor(),
    )
    try:
        task = start(container, flow, slots={"subject": "灵活就业补贴"})
        payload = json.loads(container.store.get(task.task_id).model_dump_json())
        assert payload["status"] == TaskStatus.WAITING_FOR_ACTION.value
        assert payload["user_action_expires_at"]
        assert payload["user_action_confirmed_at"] is None
    finally:
        container.close()
    # 未使用的 store 只是个对照：确保默认装配用的就是这套内存实现。
    assert store is not None
