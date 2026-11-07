"""执行预算。

"有界"是受控执行的前提：模型不能无限自我循环，也不能靠堆调用或堆上下文拖垮服务。
超预算一律按"未确认"处理，因为此时可能已经产生过副作用。

单进程里用量就是一串内存计数。跨主机部署时这串计数必须搬到共享后端：否则同一步骤
在 A 主机跑掉一半、重试落到 B 主机，B 会从零开始算，等于凭空多出一份配额；两处
同时写本地计数更是会各自"没超"。接上 `BudgetLedger` 后，用量以共享账本为准，
本地字段只是账本的镜像（用于快照与步号）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID


class BudgetExhausted(Exception):
    """预算耗尽。reason 会原样进入任务事件，便于运维定位。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class BudgetLedger(Protocol):
    """跨主机共享的用量账本。

    `reserve` 必须原子累加并返回累加后的总量；超过 `limit`（0 表示不限）时回滚
    并返回 None，让调用方按"预算耗尽"收口。后端不可用时抛异常——调用方会把它
    翻译成 `budget_store_unavailable`，结论是未确认，绝不"先跑了再说"。
    """

    def reserve(
        self, task_id: UUID, step_id: str, counter: str, delta: int, limit: int
    ) -> int | None: ...


@dataclass
class Budget:
    max_steps: int = 6
    max_tool_calls: int = 8
    deadline_seconds: float = 45.0
    # 0 表示不限制。仅在模型服务上报 usage 时才生效——不上报就无从统计。
    max_tokens: int = 0
    # 续跑时带入的已用量：预算按"整个步骤"计，不按"每次尝试"计，
    # 否则断点续跑等于凭空多出一份配额。
    initial_steps: int = 0
    initial_tool_calls: int = 0
    initial_tokens: int = 0
    # 共享账本（可选）。装配后用量以账本为准，`initial_*` 不再生效：
    # 账本里已经包含此前所有尝试的用量，再叠加一次就是重复计数。
    ledger: BudgetLedger | None = None
    task_id: UUID | None = None
    step_id: str | None = None
    _started: float = field(default=0.0, init=False, repr=False)
    _steps: int = field(default=0, init=False, repr=False)
    _tool_calls: int = field(default=0, init=False, repr=False)
    _tokens: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        self._started = time.monotonic()
        if self.ledger is not None:
            self._steps = 0
            self._tool_calls = 0
            self._tokens = 0
            return
        self._steps = max(0, self.initial_steps)
        self._tool_calls = max(0, self.initial_tool_calls)
        self._tokens = max(0, self.initial_tokens)

    @property
    def steps(self) -> int:
        return self._steps

    @property
    def tool_calls(self) -> int:
        return self._tool_calls

    @property
    def tokens(self) -> int:
        return self._tokens

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self._started

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.deadline_seconds - self.elapsed_seconds)

    @property
    def remaining_tokens(self) -> int | None:
        if self.max_tokens <= 0:
            return None
        return max(0, self.max_tokens - self._tokens)

    @property
    def remaining_tool_calls(self) -> int:
        """还剩几次工具调用额度。并发调度用它判断"整批放不放得下"。"""

        return max(0, self.max_tool_calls - self._tool_calls)

    def check_deadline(self) -> None:
        if self.remaining_seconds <= 0:
            raise BudgetExhausted("deadline")

    def _reserve(self, counter: str, delta: int, limit: int, reason: str) -> int:
        """向共享账本预约用量，返回累计总量（超限时抛 `BudgetExhausted`）。

        账本异常一律翻译成 `budget_store_unavailable`：状态不明时继续发调用，
        比一次可重试的拒绝危险得多。
        """

        ledger = self.ledger
        if ledger is None:  # pragma: no cover - 调用方已判空
            raise BudgetExhausted(reason)
        if self.task_id is None or self.step_id is None:
            # 装了账本却没给作用域：不知道记到哪个键上，绝不能猜一个。
            raise BudgetExhausted("budget_ledger_scope")
        try:
            total = ledger.reserve(self.task_id, self.step_id, counter, delta, limit)
        except BudgetExhausted:
            raise
        except Exception as exc:  # noqa: BLE001 - 后端异常类型不属于本层协议
            raise BudgetExhausted("budget_store_unavailable") from exc
        if total is None:
            raise BudgetExhausted(reason)
        return total

    def consume_step(self) -> None:
        self.check_deadline()
        if self.ledger is not None:
            self._steps = self._reserve("steps", 1, self.max_steps, "max_steps")
            return
        if self._steps >= self.max_steps:
            raise BudgetExhausted("max_steps")
        self._steps += 1

    def consume_tool_call(self) -> None:
        self.check_deadline()
        if self.ledger is not None:
            self._tool_calls = self._reserve("tool_calls", 1, self.max_tool_calls, "max_tool_calls")
            return
        if self._tool_calls >= self.max_tool_calls:
            raise BudgetExhausted("max_tool_calls")
        self._tool_calls += 1

    def reserve_tool_calls(self, count: int) -> None:
        """一次性预约整批调用，全成或全不成。

        并发批次必须要么整批扣下、要么一条都不扣：逐条 `consume_tool_call` 在半途
        失败时会留下"预算已扣了两条、实际只提交了一条"的错账，事后按用量排查会
        对不上。只可能是截止时间或次数上限让它失败，两种情况都先判后扣。
        """

        if count <= 0:
            return
        self.check_deadline()
        if self.ledger is not None:
            self._tool_calls = self._reserve(
                "tool_calls", count, self.max_tool_calls, "max_tool_calls"
            )
            return
        if self._tool_calls + count > self.max_tool_calls:
            raise BudgetExhausted("max_tool_calls")
        self._tool_calls += count

    def consume_tokens(self, tokens: int) -> None:
        """累计模型用量。超出上限时立即中止，不再发起下一轮调用。"""

        if tokens <= 0:
            return
        if self.ledger is not None:
            self._tokens = self._reserve("tokens", tokens, self.max_tokens, "max_tokens")
            return
        self._tokens += tokens
        if self.max_tokens > 0 and self._tokens > self.max_tokens:
            raise BudgetExhausted("max_tokens")

    def snapshot(self) -> dict[str, Any]:
        """预算用量快照，写入 stopped 事件供运维判断"是不是被预算掐断的"。"""

        return {
            "steps": self._steps,
            "max_steps": self.max_steps,
            "tool_calls": self._tool_calls,
            "max_tool_calls": self.max_tool_calls,
            "tokens": self._tokens,
            "max_tokens": self.max_tokens,
            "elapsed_ms": int(self.elapsed_seconds * 1000),
            "deadline_seconds": self.deadline_seconds,
            # 用量是否来自共享账本：排查"为什么这台机器的数字和断点对不上"时先看它。
            "shared_ledger": self.ledger is not None,
        }

