"""测试用的最小 Redis 服务端：只在 127.0.0.1 上开一个端口，覆盖共享状态用到的命令。

为什么不连真实 Redis：CI 里没有 Redis，装一个既拖慢流水线又把测试变成"环境依赖"。
这里用标准库 socketserver 实现 RESP2 的一小片：字符串、hash、过期、WATCH/MULTI/EXEC。
覆盖面按客户端真正发出的命令来定，多一条都不实现——假服务端比真服务端"更宽松"
是最容易骗过测试的地方。

它只用于测试：不监听非回环地址，也不做认证之外的任何安全控制。
"""

from __future__ import annotations

import socket
import socketserver
import threading
import time
from typing import Any


class ReplyError(Exception):
    """服务端错误（对应 RESP 的 `-ERR`）。"""


class FakeRedisServer:
    def __init__(self, *, password: str | None = None) -> None:
        self.password = password
        self._lock = threading.Lock()
        self._strings: dict[str, str] = {}
        self._hashes: dict[str, dict[str, int]] = {}
        self._expires: dict[str, float] = {}
        self._versions: dict[str, int] = {}
        self._sockets: list[socket.socket] = []
        self._server = socketserver.ThreadingTCPServer(
            ("127.0.0.1", 0), self._handler_class(), bind_and_activate=True
        )
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        auth = f":{self.password}@" if self.password else ""
        return f"redis://{auth}127.0.0.1:{self.port}/0"

    def stop(self) -> None:
        """模拟后端断电：先掐掉在途连接，再停监听。"""

        for sock in list(self._sockets):
            try:
                sock.close()
            except OSError:
                pass
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5.0)

    # ---------- 存储原语（handler 调用，均已加锁） ----------

    def _bump(self, key: str) -> None:
        self._versions[key] = self._versions.get(key, 0) + 1

    def _purge(self, key: str) -> None:
        deadline = self._expires.get(key)
        if deadline is None or deadline > time.monotonic():
            return
        self._strings.pop(key, None)
        self._hashes.pop(key, None)
        self._expires.pop(key, None)
        self._bump(key)

    def version(self, key: str) -> int:
        with self._lock:
            self._purge(key)
            return self._versions.get(key, 0)

    def get(self, key: str) -> str | None:
        with self._lock:
            self._purge(key)
            if key in self._hashes:
                raise ReplyError("WRONGTYPE Operation against a key holding the wrong kind")
            return self._strings.get(key)

    def keys(self) -> list[str]:
        with self._lock:
            for key in list(self._expires):
                self._purge(key)
            return sorted(set(self._strings) | set(self._hashes))

    def _handler_class(self) -> type[socketserver.BaseRequestHandler]:
        server = self

        class Handler(socketserver.StreamRequestHandler):
            def setup(self) -> None:
                super().setup()
                self.watched: dict[str, int] = {}
                self.queue: list[list[str]] | None = None
                self.authed = server.password is None
                with server._lock:
                    server._sockets.append(self.request)

            def handle(self) -> None:
                while True:
                    try:
                        parts = self._read_command()
                    except (ValueError, OSError):
                        return
                    if parts is None:
                        return
                    try:
                        reply = self._dispatch(parts)
                    except ReplyError as exc:
                        self._write(f"-{exc}\r\n".encode())
                        continue
                    self._write(self._encode(reply))

            # ---- 协议 ----

            def _read_command(self) -> list[str] | None:
                line = self.rfile.readline()
                if not line:
                    return None
                if not line.startswith(b"*"):
                    raise ValueError("inline commands are not supported")
                count = int(line[1:-2])
                parts: list[str] = []
                for _ in range(count):
                    header = self.rfile.readline()
                    if not header.startswith(b"$"):
                        raise ValueError("expected bulk string")
                    length = int(header[1:-2])
                    data = self.rfile.read(length + 2)
                    if len(data) != length + 2:
                        raise ValueError("truncated bulk string")
                    parts.append(data[:-2].decode("utf-8"))
                return parts

            def _encode(self, value: Any) -> bytes:
                if value is None:
                    return b"$-1\r\n"
                if isinstance(value, bool):
                    return b":1\r\n" if value else b":0\r\n"
                if isinstance(value, int):
                    return b":%d\r\n" % value
                if isinstance(value, str):
                    data = value.encode("utf-8")
                    return b"+%s\r\n" % data if "\n" not in value else self._bulk(data)
                if isinstance(value, list):
                    return b"*%d\r\n" % len(value) + b"".join(self._encode(item) for item in value)
                raise TypeError(f"unsupported reply type: {type(value)!r}")

            def _bulk(self, data: bytes) -> bytes:
                return b"$%d\r\n%s\r\n" % (len(data), data)

            def _write(self, data: bytes) -> None:
                try:
                    self.request.sendall(data)
                except OSError:
                    raise

            # ---- 命令 ----

            def _dispatch(self, parts: list[str]) -> Any:
                name = parts[0].upper()
                if name == "AUTH":
                    return self._auth(parts)
                if not self.authed:
                    raise ReplyError("NOAUTH Authentication required.")
                if self.queue is not None and name not in {"EXEC", "DISCARD", "MULTI", "WATCH"}:
                    self.queue.append(parts)
                    return "QUEUED"
                handler = {
                    "PING": self._ping,
                    "SELECT": lambda _: "OK",
                    "GET": self._get,
                    "SET": self._set,
                    "DEL": self._del,
                    "PEXPIRE": self._pexpire,
                    "HINCRBY": self._hincrby,
                    "WATCH": self._watch,
                    "UNWATCH": self._unwatch,
                    "MULTI": self._multi,
                    "EXEC": self._exec,
                    "DISCARD": self._discard,
                    "FLUSHALL": self._flushall,
                }.get(name)
                if handler is None:
                    raise ReplyError(f"ERR unknown command '{parts[0]}'")
                return handler(parts)

            def _auth(self, parts: list[str]) -> str:
                if server.password is None:
                    self.authed = True
                    return "OK"
                supplied = parts[-1]
                if supplied != server.password:
                    raise ReplyError("WRONGPASS invalid password")
                self.authed = True
                return "OK"

            def _ping(self, _parts: list[str]) -> str:
                return "PONG"

            def _get(self, parts: list[str]) -> Any:
                return server.get(parts[1])

            def _set(self, parts: list[str]) -> Any:
                key, value = parts[1], parts[2]
                options = [item.upper() for item in parts[3:]]
                ttl_ms: int | None = None
                only_if_absent = "NX" in options
                only_if_present = "XX" in options
                for index, option in enumerate(options):
                    if option in {"EX", "PX"}:
                        amount = int(parts[3 + index + 1])
                        ttl_ms = amount * 1000 if option == "EX" else amount
                with server._lock:
                    server._purge(key)
                    exists = key in server._strings or key in server._hashes
                    if only_if_absent and exists:
                        return None
                    if only_if_present and not exists:
                        return None
                    if key in server._hashes:
                        raise ReplyError("WRONGTYPE Operation against a key holding the wrong kind")
                    server._strings[key] = value
                    if ttl_ms is not None:
                        server._expires[key] = time.monotonic() + ttl_ms / 1000
                    else:
                        server._expires.pop(key, None)
                    server._bump(key)
                    return "OK"

            def _del(self, parts: list[str]) -> int:
                removed = 0
                with server._lock:
                    for key in parts[1:]:
                        server._purge(key)
                        if key in server._strings or key in server._hashes:
                            removed += 1
                            server._strings.pop(key, None)
                            server._hashes.pop(key, None)
                            server._expires.pop(key, None)
                            server._bump(key)
                return removed

            def _pexpire(self, parts: list[str]) -> int:
                key, ms = parts[1], int(parts[2])
                with server._lock:
                    server._purge(key)
                    if key not in server._strings and key not in server._hashes:
                        return 0
                    server._expires[key] = time.monotonic() + max(1, ms) / 1000
                    server._bump(key)
                    return 1

            def _hincrby(self, parts: list[str]) -> int:
                key, field, delta = parts[1], parts[2], int(parts[3])
                with server._lock:
                    server._purge(key)
                    if key in server._strings:
                        raise ReplyError("WRONGTYPE Operation against a key holding the wrong kind")
                    bucket = server._hashes.setdefault(key, {})
                    bucket[field] = bucket.get(field, 0) + delta
                    server._bump(key)
                    return bucket[field]

            def _watch(self, parts: list[str]) -> str:
                for key in parts[1:]:
                    self.watched[key] = server.version(key)
                return "OK"

            def _unwatch(self, _parts: list[str]) -> str:
                self.watched.clear()
                return "OK"

            def _multi(self, _parts: list[str]) -> str:
                if self.queue is not None:
                    raise ReplyError("ERR MULTI calls can not be nested")
                self.queue = []
                return "OK"

            def _discard(self, _parts: list[str]) -> str:
                self.queue = None
                self.watched.clear()
                return "OK"

            def _exec(self, _parts: list[str]) -> Any:
                if self.queue is None:
                    raise ReplyError("ERR EXEC without MULTI")
                queued, self.queue = self.queue, None
                for key, version in self.watched.items():
                    if server.version(key) != version:
                        self.watched.clear()
                        return None
                self.watched.clear()
                results = []
                for command in queued:
                    results.append(self._dispatch(command))
                return results

            def _flushall(self, _parts: list[str]) -> str:
                with server._lock:
                    server._strings.clear()
                    server._hashes.clear()
                    server._expires.clear()
                    server._versions.clear()
                return "OK"

        return Handler

