"""跨主机共享状态：租约、取消信号、预算账本共用的最小原语面。

为什么单独抽一层：这三件事各自已经有同机实现（SQLite 文件 / 进程内内存），
但它们跨主机时需要的是同一类东西——一个**带 TTL 的原子键值面**。把差异收敛到
5 个原语，共享后端只实现这一层，三处的业务语义（谁在跑、取消没取消、用掉多少）
不用各写一遍。

原语语义统一按 fail-closed 定义：

- 抢不到（键被别人持有）返回 `False`，这是正常的业务结果，不是错误；
- 后端不可用、超时、协议错误一律抛 `SharedStateError`。调用方必须按"拒绝执行 /
  停止动作"处理——把"不知道"当成"没有"才是真正危险的；
- 比较删除（`release`）只删自己持有的值，不能删掉别人的租约：租约过期后被别人
  接管时，迟到的释放如果直接 DEL，就会把新持有者的保护一起抹掉。

这层不推进业务、不保存业务数据：键值里只有 owner 随机串、取消原因和预算计数。
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

# 键前缀可配置：同一个 Redis 可能被多套环境共用，前缀是唯一的分隔手段。
DEFAULT_KEY_PREFIX = "workpanda"


class SharedStateError(RuntimeError):
    """共享后端不可用或返回了无法解释的结果。调用方一律按拒绝处理。"""


class SharedStateStore(Protocol):
    """跨主机共享状态后端。实现必须保证单个原语是原子的。"""

    def try_acquire(self, key: str, owner: str, ttl_seconds: float) -> bool:
        """键不存在（或已过期、或本来就属于 owner）时占住它并返回 True。"""
        ...

    def release(self, key: str, owner: str) -> bool:
        """仅当当前值仍是 owner 时删除。返回是否真的删掉了。"""
        ...

    def put(self, key: str, value: str, ttl_seconds: float) -> None:
        """覆盖写入并续期。写失败必须抛 `SharedStateError`。"""
        ...

    def get(self, key: str) -> str | None:
        """读取当前值；不存在或已过期返回 None。读失败必须抛 `SharedStateError`。"""
        ...

    def add(self, key: str, field: str, delta: int, ttl_seconds: float) -> int:
        """把 `field` 原子累加 `delta` 并返回累加后的值。"""
        ...


def lease_key(prefix: str, task_id: UUID, step_id: str) -> str:
    return f"{prefix}:lease:{task_id}:{step_id}"


def cancel_key(prefix: str, task_id: UUID) -> str:
    return f"{prefix}:cancel:{task_id}"


def budget_key(prefix: str, task_id: UUID, step_id: str) -> str:
    return f"{prefix}:budget:{task_id}:{step_id}"
