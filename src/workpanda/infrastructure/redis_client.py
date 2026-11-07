"""最小的 Redis 客户端：只为共享状态那 5 个原语服务。

为什么自己写而不是加依赖：共享状态只在显式配置 `harness_shared_backend=redis`
时才启用，绝大多数部署（单机、同机多进程）根本不需要 Redis。为一条可选路径
给整个项目引入一个运行时依赖，会让"没启用功能的人"也承担升级与供应链风险。
这里只用标准库 socket 说 RESP2，命令面固定且很少，测试里配一个假服务端即可
完整覆盖——不需要 CI 里出现真实 Redis。

两条安全边界：

1. **不做自动重试**：写命令（`HINCRBY`、`SET`）超时后结果未知，重发可能把预算
   算两遍、或者把已经放掉的租约再放一次。传输层出错就丢掉连接、如实抛
   `SharedStateError`，由上层按拒绝处理；下一次调用会重新建连。
2. **不静默降级**：TLS、集群、哨兵这些形态这里不支持，遇到 `rediss://` 直接报错，
   不假装连上了。
"""

from __future__ import annotations

import socket
from threading import Lock
from typing import Any
from urllib.parse import urlsplit

from workpanda.harness.shared_state import SharedStateError

DEFAULT_TIMEOUT_SECONDS = 0.5
# 单条回复的上限保护：服务端异常或被中间设备改写时，不要让解析器无限读下去。
MAX_BULK_BYTES = 8 * 1024 * 1024


class RedisReplyError(SharedStateError):
    """服务端明确报错（例如 WRONGTYPE）。与"连不上"要分开，便于定位。"""


class RedisTarget:
    """`redis://[:password@]host:port[/db]` 的解析结果。"""

    __slots__ = ("db", "host", "password", "port", "username")

    def __init__(
        self,
        host: str,
        port: int,
        *,
        password: str | None = None,
        username: str | None = None,
        db: int = 0,
    ) -> None:
        self.host = host
        self.port = port
        self.password = password
        self.username = username
        self.db = db

    @classmethod
    def parse(cls, url: str) -> RedisTarget:
        parts = urlsplit(url.strip())
        if parts.scheme == "rediss":
            raise SharedStateError("rediss://（TLS）尚不支持，请使用受信网络内的 redis://")
        if parts.scheme != "redis":
            raise SharedStateError(f"不支持的 Redis URL 协议：{parts.scheme or '(空)'}")
        if not parts.hostname:
            raise SharedStateError("Redis URL 缺少主机名")
        raw_db = parts.path.strip("/")
        try:
            db = int(raw_db) if raw_db else 0
        except ValueError as exc:
            raise SharedStateError(f"Redis URL 的库编号不合法：{raw_db}") from exc
        return cls(
            parts.hostname,
            parts.port or 6379,
            password=parts.password,
            username=parts.username or None,
            db=db,
        )

    @property
    def describe(self) -> str:
        return f"{self.host}:{self.port}/{self.db}"


def _encode(parts: tuple[str, ...]) -> bytes:
    chunks = [b"*%d\r\n" % len(parts)]
    for part in parts:
        data = part.encode("utf-8")
        chunks.append(b"$%d\r\n" % len(data) + data + b"\r\n")
    return b"".join(chunks)


def _read_reply(stream: Any) -> Any:
    line = stream.readline()
    if not line:
        raise SharedStateError("Redis 连接被对端关闭")
    kind, payload = line[:1], line[1:-2]
    if kind == b"+":
        return payload.decode("utf-8")
    if kind == b"-":
        raise RedisReplyError(payload.decode("utf-8", "replace"))
    if kind == b":":
        return int(payload)
    if kind == b"$":
        length = int(payload)
        if length == -1:
            return None
        if length > MAX_BULK_BYTES:
            raise SharedStateError("Redis 回复长度异常")
        data = stream.read(length + 2)
        if len(data) != length + 2:
            raise SharedStateError("Redis 回复被截断")
        return data[:-2].decode("utf-8")
    if kind == b"*":
        count = int(payload)
        if count == -1:
            return None
        return [_read_reply(stream) for _ in range(count)]
    raise SharedStateError(f"无法解析的 Redis 回复：{line[:32]!r}")


class RedisClient:
    """单连接、带锁的同步客户端。

    连接是长连（每次边界检查都建连会把取消轮询变成 TLS 握手风暴），但任何
    传输层异常都会把连接丢掉：宁可下一次重连，也不要在一个状态不明的连接上
    继续发写命令。
    """

    def __init__(self, url: str, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self._target = RedisTarget.parse(url)
        self._timeout = max(0.05, timeout_seconds)
        self._lock = Lock()
        self._sock: socket.socket | None = None
        self._fp: Any = None

    @property
    def target(self) -> RedisTarget:
        return self._target

    # ---------- 连接 ----------

    def _connect_locked(self) -> None:
        try:
            sock = socket.create_connection(
                (self._target.host, self._target.port), timeout=self._timeout
            )
        except OSError as exc:
            raise SharedStateError(f"无法连接 Redis {self._target.describe}：{exc}") from exc
        sock.settimeout(self._timeout)
        self._sock = sock
        self._fp = sock.makefile("rb")
        try:
            if self._target.password is not None:
                if self._target.username:
                    self._execute_locked("AUTH", self._target.username, self._target.password)
                else:
                    self._execute_locked("AUTH", self._target.password)
            if self._target.db:
                self._execute_locked("SELECT", str(self._target.db))
        except BaseException:
            self._drop_locked()
            raise

    def _drop_locked(self) -> None:
        for closer in (self._fp, self._sock):
            try:
                if closer is not None:
                    closer.close()
            except OSError:
                pass
        self._fp = None
        self._sock = None

    # ---------- 命令 ----------

    def _execute_locked(self, *parts: str) -> Any:
        if self._sock is None:
            self._connect_locked()
        try:
            self._sock.sendall(_encode(parts))  # type: ignore[union-attr]
        except OSError as exc:
            raise SharedStateError(f"Redis 写入失败：{exc}") from exc
        return _read_reply(self._fp)

    def execute(self, *parts: str) -> Any:
        """执行一条命令。传输层出错时丢连接并抛 `SharedStateError`。"""

        with self._lock:
            try:
                return self._execute_locked(*parts)
            except RedisReplyError:
                # 服务端明确拒绝，连接本身是好的，不需要重连。
                raise
            except SharedStateError:
                self._drop_locked()
                raise
            except (OSError, ValueError, EOFError) as exc:
                self._drop_locked()
                raise SharedStateError(f"Redis 命令失败：{exc}") from exc

    def compare_and_delete(self, key: str, expected: str) -> bool:
        """仅当值仍是 `expected` 时删除。用 WATCH/MULTI 保证"比较"与"删除"原子。"""

        with self._lock:
            try:
                self._execute_locked("WATCH", key)
                current = self._execute_locked("GET", key)
                if current != expected:
                    self._execute_locked("UNWATCH")
                    return False
                self._execute_locked("MULTI")
                self._execute_locked("DEL", key)
                result = self._execute_locked("EXEC")
                # EXEC 返回 None 表示事务被放弃：值在我们比较之后变了，
                # 此时"没删掉"就是正确结果，交给 TTL 兜底。
                return result is not None
            except RedisReplyError:
                self._drop_locked()
                raise
            except SharedStateError:
                self._drop_locked()
                raise
            except (OSError, ValueError, EOFError) as exc:
                self._drop_locked()
                raise SharedStateError(f"Redis 事务失败：{exc}") from exc

    def ping(self) -> None:
        """启动时探活：连不上就不要带着"以为共享着"的配置启动。"""

        reply = self.execute("PING")
        if reply != "PONG":
            raise SharedStateError(f"Redis 探活返回异常：{reply!r}")

    def close(self) -> None:
        with self._lock:
            self._drop_locked()
