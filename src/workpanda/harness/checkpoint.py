"""断点续跑：把一次执行的中间状态落盘，崩溃或重启后从断点继续。

为什么必须有：一次步骤可能跑多轮模型、多个工具。进程在中途挂掉时，此前发生的
工具调用既不能重来（可能重复副作用），也不该整段丢弃（用户已经确认过的动作不该
再走一遍）。没有 checkpoint 时，唯一的选择是整步失败并转人工。

三条安全边界：

1. **不确定就不续跑**：任何工具出现 `TIMEOUT`（结果未知）都会把 checkpoint 标成
   不可续跑——无法判断外部系统是否已执行，此时续跑等于盲目重试；
2. **恢复授权状态本身**：checkpoint 保存已成功副作用指纹与各工具已用次数，
   续跑时原样恢复，因此已经提交过的非幂等操作会被拦为 `duplicate_side_effect`；
3. **同库同权限**：checkpoint 与任务槽位落在同一个库、同一套权限下，不额外扩大
   数据面；该能力默认关闭，需要显式开启并接受其留存影响。

checkpoint 只描述"执行到哪一步"，不参与流程推进，也不能替代 Task Store。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel, Field

from workpanda.harness.models import HarnessMessage, ToolCallRecord


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ExecutionSnapshot(BaseModel):
    """一次执行在某一时刻的完整可恢复状态。

    `messages` 是回灌给模型的原始对话（含工具结果），必须原样保存：
    脱敏后的值会让模型拿着掩码继续推理。
    """

    messages: list[HarnessMessage] = Field(default_factory=list)
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    steps: int = Field(default=0, ge=0)
    tool_call_count: int = Field(default=0, ge=0)
    tokens: int = Field(default=0, ge=0)


class HarnessCheckpoint(BaseModel):
    """某个任务某个步骤的断点。同一 (task_id, step_id) 只保留最新一份。"""

    trace_id: str = Field(min_length=1, max_length=64)
    task_id: UUID
    flow_id: str
    flow_version: str
    step_id: str
    # 授权集合必须一致才能续跑：换了工具就等于换了执行语义。
    allowed_tools: list[str] = Field(default_factory=list)
    snapshot: ExecutionSnapshot
    # ToolGrant 的执行期状态（已用次数、已成功副作用指纹），用于防止重复副作用。
    grant_state: dict[str, Any] = Field(default_factory=dict)
    # False 表示这次执行出现过"结果未知"，禁止续跑，只能由人工核实。
    resumable: bool = True
    blocked_reason: str | None = None
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)

    def is_expired(self, ttl_seconds: float, *, now: datetime | None = None) -> bool:
        if ttl_seconds <= 0:
            return False
        current = now or _utc_now()
        return current - self.updated_at > timedelta(seconds=ttl_seconds)


class CheckpointStore(Protocol):
    def save(self, checkpoint: HarnessCheckpoint) -> None: ...

    def load(self, task_id: UUID, step_id: str) -> HarnessCheckpoint | None: ...

    def delete(self, task_id: UUID, step_id: str) -> None: ...


class InMemoryCheckpointStore:
    """进程内实现：只能支撑"同一进程内重试"，重启即失效。"""

    def __init__(self) -> None:
        self._items: dict[tuple[UUID, str], HarnessCheckpoint] = {}
        self._lock = Lock()

    def save(self, checkpoint: HarnessCheckpoint) -> None:
        with self._lock:
            self._items[(checkpoint.task_id, checkpoint.step_id)] = checkpoint

    def load(self, task_id: UUID, step_id: str) -> HarnessCheckpoint | None:
        with self._lock:
            return self._items.get((task_id, step_id))

    def delete(self, task_id: UUID, step_id: str) -> None:
        with self._lock:
            self._items.pop((task_id, step_id), None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)
