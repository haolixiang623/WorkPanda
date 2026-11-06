"""执行预算。

"有界"是受控执行的前提：模型不能无限自我循环，也不能靠堆调用拖垮服务。
超预算一律按"未确认"处理，因为此时可能已经产生过副作用。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


class BudgetExhausted(Exception):
    """预算耗尽。reason 会原样进入任务事件，便于运维定位。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class Budget:
    max_steps: int = 6
    max_tool_calls: int = 8
    deadline_seconds: float = 45.0
    _started: float = field(default=0.0, init=False, repr=False)
    _steps: int = field(default=0, init=False, repr=False)
    _tool_calls: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        self._started = time.monotonic()

    @property
    def steps(self) -> int:
        return self._steps

    @property
    def tool_calls(self) -> int:
        return self._tool_calls

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self._started

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.deadline_seconds - self.elapsed_seconds)

    def check_deadline(self) -> None:
        if self.remaining_seconds <= 0:
            raise BudgetExhausted("deadline")

    def consume_step(self) -> None:
        self.check_deadline()
        if self._steps >= self.max_steps:
            raise BudgetExhausted("max_steps")
        self._steps += 1

    def consume_tool_call(self) -> None:
        self.check_deadline()
        if self._tool_calls >= self.max_tool_calls:
            raise BudgetExhausted("max_tool_calls")
        self._tool_calls += 1
