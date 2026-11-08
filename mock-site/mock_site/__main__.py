"""`python -m mock_site`：同时起账号站与办理站。

用法（在 `mock-site/` 目录内）：

```bash
python -m mock_site                 # 8101 / 8102，两个 origin
python -m mock_site --check         # 只校验配置与端口占用
python -m mock_site --submit-mode error_but_accepted
```

不带参数时也会读 `MOCK_SITE_*` 环境变量；两者同时存在时命令行优先。
"""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
from dataclasses import replace

import uvicorn
from fastapi import FastAPI

from mock_site.app import build_apps
from mock_site.config import SUBMIT_MODES, MockConfig, MockConfigError


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m mock_site",
        description="WorkPanda 本地模拟三方站（模拟站，不代表任何真实政务系统）",
    )
    parser.add_argument("--account-port", type=int, default=None, help="账号站端口（默认 8101）")
    parser.add_argument("--apply-port", type=int, default=None, help="办理站端口（默认 8102）")
    parser.add_argument("--submit-mode", choices=SUBMIT_MODES, default=None, help="提交行为开关")
    parser.add_argument("--check", action="store_true", help="只校验配置与端口占用后退出")
    return parser.parse_args(argv)


def _resolve_config(args: argparse.Namespace) -> MockConfig:
    config = MockConfig.from_env()
    changes: dict[str, object] = {}
    if args.account_port is not None:
        changes["account_port"] = args.account_port
    if args.apply_port is not None:
        changes["apply_port"] = args.apply_port
    if args.submit_mode is not None:
        changes["submit_mode"] = args.submit_mode
    return replace(config, **changes) if changes else config


def _port_problems(host: str, ports: tuple[int, ...]) -> list[str]:
    problems: list[str] = []
    for port in ports:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind((host, port))
            except OSError as exc:
                problems.append(f"{host}:{port} 不可用：{exc}")
    return problems


def _start(
    app: FastAPI, *, host: str, port: int, name: str
) -> tuple[uvicorn.Server, threading.Thread]:
    config = uvicorn.Config(
        app, host=host, port=port, log_level="info", access_log=False, lifespan="off"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name=f"mock-site-{name}", daemon=True)
    thread.start()
    return server, thread


def _wait_started(
    server: uvicorn.Server, thread: threading.Thread, *, timeout: float = 5.0
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server.started:
            return True
        if not thread.is_alive():
            return False
        time.sleep(0.02)
    return False


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        config = _resolve_config(args)
    except MockConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2

    host = config.bind_host
    ports = (config.account_port, config.apply_port)
    problems = _port_problems(host, ports)
    if problems:
        print("端口不可用：\n  " + "\n  ".join(problems), file=sys.stderr)
        return 3
    if args.check:
        print(f"配置与端口均可：账号站 {config.account_origin}，办理站 {config.apply_origin}")
        print(f"提交模式 {config.submit_mode}；账号 {', '.join(sorted(config.accounts))}")
        return 0

    account_app, apply_app, _state = build_apps(config)
    account_server, account_thread = _start(
        account_app, host=host, port=config.account_port, name="account"
    )
    apply_server, apply_thread = _start(apply_app, host=host, port=config.apply_port, name="apply")

    if not _wait_started(account_server, account_thread) or not _wait_started(
        apply_server, apply_thread
    ):
        print("启动失败：有站点没有进入监听状态", file=sys.stderr)
        account_server.should_exit = True
        apply_server.should_exit = True
        return 3

    print("本地模拟站已启动（只监听回环，不联网）")
    print(f"  账号站  {config.account_origin}/login")
    print(f"  办理站  {config.apply_origin}/apply   ← 另一个 origin")
    print(f"  演示账号  {', '.join(sorted(config.accounts))}")
    print(f"  提交模式  {config.submit_mode}")
    print("  Ctrl+C 退出")
    try:
        while account_thread.is_alive() and apply_thread.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n正在停止……")
        account_server.should_exit = True
        apply_server.should_exit = True
    finally:
        account_thread.join(timeout=5)
        apply_thread.join(timeout=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
