"""按主体限流，避免单个会话耗尽服务资源。"""

from dataclasses import dataclass, field
from threading import Lock
from time import monotonic


@dataclass
class _Bucket:
    tokens: float
    updated_at: float


@dataclass
class TokenBucketLimiter:
    """令牌桶：`per_minute` 决定稳态速率，`burst` 决定瞬时上限。"""

    per_minute: int = 120
    burst: int | None = None
    _buckets: dict[str, _Bucket] = field(default_factory=dict)
    _lock: Lock = field(default_factory=Lock)

    def __post_init__(self) -> None:
        if self.per_minute <= 0:
            raise ValueError("per_minute must be positive")
        if self.burst is None:
            self.burst = max(1, self.per_minute // 4)
        if self.burst <= 0:
            raise ValueError("burst must be positive")

    @property
    def _rate_per_second(self) -> float:
        return self.per_minute / 60.0

    def allow(self, key: str, cost: float = 1.0) -> bool:
        now = monotonic()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(tokens=float(self.burst), updated_at=now)
                self._buckets[key] = bucket
            else:
                elapsed = max(0.0, now - bucket.updated_at)
                bucket.tokens = min(
                    float(self.burst), bucket.tokens + elapsed * self._rate_per_second
                )
                bucket.updated_at = now

            if bucket.tokens < cost:
                return False
            bucket.tokens -= cost
            return True

    def retry_after_seconds(self, key: str) -> int:
        with self._lock:
            bucket = self._buckets.get(key)
            missing = 1.0 if bucket is None else max(0.0, 1.0 - bucket.tokens)
        return max(1, int(missing / self._rate_per_second) + 1)
