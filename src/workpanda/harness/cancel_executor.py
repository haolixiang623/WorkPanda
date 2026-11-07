"""让"取消"这道边界对任意执行器生效。

问题：取消信号目前只有 `HarnessExecutor` 自己的执行循环在读。换成 Octop 运行时
或脚手架执行器，`POST /api/tasks/{id}/cancel` 就只剩翻任务状态——正在跑的那一步
照样把模型调用和工具调用做完，包括用户刚说"不要了"的那一次提交。这正是
`docs/HARNESS.md` 第 14 节写下的已知限制：取消对它们仍是纯状态翻转。

这里补的是四道边界里的第一道，也是**唯一能对任意执行器作出承诺**的一道：
**执行开始前**。包装器在把请求交给内层之前检查一次取消；已取消就一步都不跑、
连模型都不调，直接给出 `unconfirmed` + `cancelled`。

刻意不做的事（写清楚，免得被当成万能）：

- 请求交给内层之后，本包装器无法知道它跑到哪一步，也没有打断它的手段。
  内层自己读令牌（`HarnessExecutor` 就是这样）就继续生效；不读的话，
  这一次执行的粒度仍是"跑完整个步骤"。
- 因此"中途取消"的承诺仍然是逐执行器而定的。本包装器统一的是开始前那一道，
  它挡住的正是"用户已经点了取消、这一笔却还是从头开始跑"这种最容易避免的浪费。
- 内层返回什么就报告什么，不因为"期间收到过取消"而改写结论：执行已经发生，
  它到底做了什么只有内层知道，替它改判等于制造一个没人能核实的记录。
  取消与迟到结果之间的仲裁仍由 `FlowRunner` 的条件写负责。
"""

from __future__ import annotations

import time

from workpanda.application.ports import (
    ExecutionOutcome,
    ExecutionRequest,
    ExecutionResult,
    Executor,
)
from workpanda.harness.cancel import CancelRegistry, CancelToken


class CancellableExecutor(Executor):
    """给任何执行器补上"取消先于执行"这道边界。

    装配层把取消登记表与执行器一起交给它即可；登记表必须与取消方（HTTP 线程）
    看到的是同一个对象，否则本包装器读到的永远是"没人取消"。

    不要用它包裹已经在读令牌的 `HarnessExecutor`：两层会为同一个
    `(task_id, step_id)` 各取一次令牌，内层收尾时就把外层还需要的登记项释放了。
    那一层已经自带取消，直接使用即可。
    """

    def __init__(self, inner: Executor, cancellations: CancelRegistry) -> None:
        self._inner = inner
        self._cancellations = cancellations

    @property
    def inner(self) -> Executor:
        """暴露内层执行器，便于装配层关闭它持有的资源。"""

        return self._inner

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        started = time.monotonic()
        token = self._cancellations.begin(request.task_id, request.step_id)
        try:
            if token.cancelled:
                return self._cancelled_before_start(request, token, started)
            return self._inner.execute(request)
        finally:
            try:
                self._cancellations.end(request.task_id, request.step_id)
            except Exception:  # noqa: BLE001 - 释放失败只影响内存占用，不该击穿结论
                pass

    def _cancelled_before_start(
        self, request: ExecutionRequest, token: CancelToken, started: float
    ) -> ExecutionResult:
        """一步都不跑，但结论与留痕要和 harness 自己拒绝时长得一样。

        `FlowRunner` 会把 `stop_reason` 与 `verification` 抄进任务事件，
        审计归档也按 `stop_reason=cancelled` 记账；形状不一致会让运维看到
        "取消"有两种互不相认的写法。
        """

        message = f"本次执行在开始前已被取消（{token.reason}），未调用模型或工具。"
        return ExecutionResult(
            outcome=ExecutionOutcome.UNCONFIRMED,
            output={
                "tool": request.tool,
                "step_id": request.step_id,
                "text": "",
                "verification": {"reason": "cancelled", "summary": message},
                "tool_calls": [],
                "model_calls": 0,
                "tokens": 0,
                "duration_ms": int((time.monotonic() - started) * 1000),
                "stop_reason": "cancelled",
            },
            error_code="cancelled",
            message=message,
        )
