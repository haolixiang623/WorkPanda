"""共享状态的 Redis 实现：租约、取消信号、预算账本三处共用同一层原语。

键的布局（前缀可用 `harness_shared_key_prefix` 区分环境）：

| 键 | 类型 | 含义 |
|---|---|---|
| `<prefix>:lease:<task>:<step>` | string | 当前执行者随机串，带 PX 过期 |
| `<prefix>:cancel:<task>` | string | 取消原因，带 PX 过期 |
| `<prefix>:budget:<task>:<step>` | hash | 步数 / 工具调用 / token 用量 |

为什么三处都用同一套键面：它们要回答的是同一个问题——"这一步现在归谁、被叫停
没有、还剩多少额度"。放在同一个后端里，运维只需要看一处，容量与过期策略也统一。

fail-closed 的三条落点：

- 租约：任何异常上抛，执行器按"抢不到"拒绝；
- 取消：写失败上抛 → 接口返回 503，任务状态不翻转；读失败 → 令牌按已取消收口；
- 预算：累加失败按预算耗尽处理（结论是未确认），绝不"先跑了再说"。
"""

from __future__ import annotations

from uuid import UUID

from workpanda.harness.shared_state import (
    DEFAULT_KEY_PREFIX,
    SharedStateStore,
    budget_key,
    cancel_key,
    lease_key,
)
from workpanda.infrastructure.redis_client import RedisClient

# 小于 1ms 的 TTL 在 Redis 里等于"立刻过期"，会让租约完全失效。
MIN_TTL_MS = 1


def _ttl_ms(ttl_seconds: float) -> int:
    return max(MIN_TTL_MS, int(max(0.0, ttl_seconds) * 1000))


class RedisSharedState(SharedStateStore):
    """把 5 个原语映射到四条命令（SET NX / GET / PEXPIRE / HINCRBY + WATCH 事务）。"""

    def __init__(self, client: RedisClient, *, prefix: str = DEFAULT_KEY_PREFIX) -> None:
        self._client = client
        self._prefix = prefix or DEFAULT_KEY_PREFIX

    @property
    def prefix(self) -> str:
        """键前缀。三个适配器用它拼键，避免各自再读私有字段。"""

        return self._prefix

    def try_acquire(self, key: str, owner: str, ttl_seconds: float) -> bool:
        ttl = _ttl_ms(ttl_seconds)
        reply = self._client.execute("SET", key, owner, "PX", str(ttl), "NX")
        if reply == "OK":
            return True
        # 没抢到有两种可能：别人持有（正常拒绝），或本来就是我持有（重入）。
        # 只看 SET 的返回值分不清，必须再读一次；读出来的不是自己就绝不续期。
        if self._client.execute("GET", key) != owner:
            return False
        self._client.execute("PEXPIRE", key, str(ttl))
        # 续期与读取之间可能已过期并被别人抢走，再看一眼收口。多一次读的代价
        # 远小于"把别人的租约续上、让两个执行者并存"。
        return self._client.execute("GET", key) == owner

    def release(self, key: str, owner: str) -> bool:
        return self._client.compare_and_delete(key, owner)

    def put(self, key: str, value: str, ttl_seconds: float) -> None:
        self._client.execute("SET", key, value, "PX", str(_ttl_ms(ttl_seconds)))

    def get(self, key: str) -> str | None:
        value = self._client.execute("GET", key)
        return None if value is None else str(value)

    def add(self, key: str, field: str, delta: int, ttl_seconds: float) -> int:
        total = int(self._client.execute("HINCRBY", key, field, str(delta)))
        # 每次累加都续期：一次执行可能跨很多次调用，固定 TTL 会在中途过期，
        # 过期就等于"预算凭空重置"。
        self._client.execute("PEXPIRE", key, str(_ttl_ms(ttl_seconds)))
        return total

    def close(self) -> None:
        self._client.close()


class RedisStepLeaseStore:
    """跨主机步骤租约。同一 `(task_id, step_id)` 在任意主机上只有一个持有者。"""

    def __init__(self, state: RedisSharedState) -> None:
        self._state = state

    def acquire(self, task_id: UUID, step_id: str, owner: str, ttl_seconds: float) -> bool:
        return self._state.try_acquire(
            lease_key(self._state.prefix, task_id, step_id), owner, ttl_seconds
        )

    def release(self, task_id: UUID, step_id: str, owner: str) -> None:
        self._state.release(lease_key(self._state.prefix, task_id, step_id), owner)


class RedisCancelSignalStore:
    """跨主机取消信号。写失败上抛，读失败同样上抛，由令牌按已取消收口。"""

    def __init__(self, state: RedisSharedState) -> None:
        self._state = state

    def signal(self, task_id: UUID, reason: str, *, ttl_seconds: float) -> None:
        self._state.put(cancel_key(self._state.prefix, task_id), reason, ttl_seconds)

    def reason_for(self, task_id: UUID) -> str | None:
        return self._state.get(cancel_key(self._state.prefix, task_id))

    def clear(self, task_id: UUID) -> None:
        """人工确认后清理。等价于等 TTL 到期，但不必等。"""

        key = cancel_key(self._state.prefix, task_id)
        current = self._state.get(key)
        if current is not None:
            self._state.release(key, current)


class RedisBudgetLedger:
    """跨主机预算账本：用量存在共享后端里，续跑/换主机都不会凭空多出配额。

    超限时先扣再退：宁可让并发的另一边看到"暂时超额"而误拒（fail-closed），
    也不能先判定后扣——那会在一瞬间放进超出上限的调用。
    """

    def __init__(
        self,
        state: RedisSharedState,
        *,
        ttl_seconds: float,
    ) -> None:
        self._state = state
        self._ttl_seconds = max(1.0, ttl_seconds)

    def reserve(
        self, task_id: UUID, step_id: str, counter: str, delta: int, limit: int
    ) -> int | None:
        if delta <= 0:
            return 0
        key = budget_key(self._state.prefix, task_id, step_id)
        total = self._state.add(key, counter, delta, self._ttl_seconds)
        # `limit` 为负才是"不限"；0 是"一点额度都没有"，必须照常拒绝。
        # 把 0 当不限会静默抹掉上限：进程内 0 步立刻拒绝，账本里却放行任意多。
        if limit >= 0 and total > limit:
            self._state.add(key, counter, -delta, self._ttl_seconds)
            return None
        return total
