"""副作用账本：把"这一步对外部世界做了什么"从调用记录里提炼成可核对的条目。

为什么单独成一层：调用记录回答的是"模型让哪个工具跑了、返回了什么"，
而事故复盘要回答的是两个不同的问题——

1. 外部系统**确定**被改动了什么？（已提交的单据、已发出的通知）
2. 有没有"可能改了、但没拿到回执"的动作？（超时、断连）

这两件事不能用同一套判断：幂等的查询跑得再慢也不构成副作用，
非幂等的提交只要没拿到返回就必须按"可能已执行"处理。
因此这里只认两种状态：`applied`（确定已发生）与 `unknown`（可能已发生，须人工核实）。

边界：本模块是纯函数，不落盘、不读存储、不认识业务；落盘由 `audit.py` 负责。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from workpanda.harness.models import (
    RiskLevel,
    ToolCallRecord,
    ToolOutcome,
    ToolSpec,
    utc_now,
)


class EffectStatus(StrEnum):
    # 非幂等调用成功返回：外部系统确定执行过。
    APPLIED = "applied"
    # 非幂等调用超时：此刻无法判断外部系统是否已执行，只能人工核对。
    UNKNOWN = "unknown"


class EffectEntry(BaseModel):
    """一笔待核对的外部动作。

    `fingerprint` 是"工具名 + 参数"的摘要值，而不是参数原文：
    它能回答"两次调用是不是同一个动作"，但不会把业务数据写进审计文件。
    """

    trace_id: str = Field(min_length=1, max_length=64)
    task_id: UUID
    flow_id: str = ""
    flow_version: str = ""
    step_id: str = ""
    tool: str
    status: EffectStatus
    risk: RiskLevel = RiskLevel.LOW
    call_id: str = ""
    fingerprint: str = ""
    error_code: str | None = None
    attempts: int = 1
    occurred_at: datetime = Field(default_factory=utc_now)

    @property
    def needs_manual_check(self) -> bool:
        return self.status is EffectStatus.UNKNOWN


class EffectSummary(BaseModel):
    applied: int = 0
    unknown: int = 0

    @property
    def requires_manual_check(self) -> bool:
        """只要有一笔"结果未知"的外部动作，这一步就不能算交付完成。"""

        return self.unknown > 0


def fingerprint_of(name: str, arguments: Mapping[str, Any]) -> str:
    """参数指纹：稳定、可比对、不可逆。

    刻意不用 `repr(dict)`：键序不同会导致同一动作算出不同指纹，
    而"同一动作"的判断正是防重复副作用的基础。
    """

    try:
        payload = json.dumps(dict(arguments), ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        payload = repr(sorted((str(key) for key in arguments), key=str))
    digest = hashlib.sha256(f"{name}\n{payload}".encode())
    return digest.hexdigest()[:16]


def build_effects(
    records: Sequence[ToolCallRecord],
    *,
    specs: Mapping[str, ToolSpec] | None = None,
    trace_id: str,
    task_id: UUID,
    flow_id: str = "",
    flow_version: str = "",
    step_id: str = "",
) -> list[EffectEntry]:
    """从调用记录里挑出真正改动外部世界的动作。

    判定口径（宁可多一条待核对，也不要漏掉一次真实提交）：

    - 声明的幂等工具（查询类）一律不算副作用，包括它超时的情况；
    - 未声明 `specs` 里的工具按"可能有副作用"处理——登记表缺失时保守对待；
    - 成功 → `applied`；超时 → `unknown`；
    - 被拦（未授权/参数不合法/重复提交）与工具自报失败 → 明确没有执行，不入账。
    """

    catalog = specs or {}
    effects: list[EffectEntry] = []
    for record in records:
        spec = catalog.get(record.name)
        if spec is not None and spec.idempotent:
            continue
        if record.outcome is ToolOutcome.OK:
            status = EffectStatus.APPLIED
        elif record.outcome is ToolOutcome.TIMEOUT:
            status = EffectStatus.UNKNOWN
        else:
            # 被拦或工具明确报错：外部系统没有被改动，不需要人工去核实。
            continue
        effects.append(
            EffectEntry(
                trace_id=trace_id,
                task_id=task_id,
                flow_id=flow_id,
                flow_version=flow_version,
                step_id=step_id,
                tool=record.name,
                status=status,
                risk=record.risk,
                call_id=record.call_id,
                fingerprint=fingerprint_of(record.name, record.arguments),
                error_code=record.error_code,
                attempts=record.attempts,
            )
        )
    return effects


def summarize_effects(effects: Iterable[EffectEntry]) -> EffectSummary:
    applied = 0
    unknown = 0
    for effect in effects:
        if effect.status is EffectStatus.APPLIED:
            applied += 1
        else:
            unknown += 1
    return EffectSummary(applied=applied, unknown=unknown)
