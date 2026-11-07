"""把 WorkPanda 试点实例从 /tmp 固化为持久部署（macOS launchd + 登录钥匙串）。

设计约束（来自 Issue #29）：

- 端口固定：Octop 8090、WorkPanda 8010；本机 8088 / 8000 已被别的服务占用。
- 只监听回环地址。**不提供** 改成 ``0.0.0.0``、也不开 TLS 的开关 —— 对外暴露
  属于安全边界变更，不在本工具的职责内。
- 凭据只进登录钥匙串，任何文件都不落盘明文；生成的 plist 里只有路径和端口。
- 状态目录放在 ``~/Library/Application Support/WorkPanda``，机器重启由 launchd
  按 ``RunAtLoad`` + ``KeepAlive`` 拉起，不依赖任何手工后台进程。

常用命令见 ``deploy/README.md``。
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

API_LABEL = "com.workpanda.api"
OCTOP_LABEL = "com.workpanda.octop"
API_PORT = 8010
OCTOP_PORT = 8090
BIND_HOST = "127.0.0.1"
KEYCHAIN_SERVICE = "workpanda-deploy"
KEYCHAIN_TOOL = "/usr/bin/security"
DEFAULT_OCTOP_VERSION = "1.0.2"

# 需要从钥匙串注入的变量。凭据只按名字出现，值永远只在内存里。
API_SECRET_NAMES = (
    "WORKPANDA_OPS_TOKEN",
    "WORKPANDA_SESSION_SECRET",
    "WORKPANDA_MODEL_API_KEY",
    "WORKPANDA_OCTOP_API_KEY",
)
OCTOP_SECRET_NAMES = ("WORKPANDA_OPS_TOKEN",)
OPTIONAL_SECRET_NAMES = ("OCTOP_ADMIN_PASSWORD",)
ALL_SECRET_NAMES = (*API_SECRET_NAMES, *OCTOP_SECRET_NAMES, *OPTIONAL_SECRET_NAMES)

# api.env 允许的键：只放非敏感配置。敏感键出现即拒绝，避免有人图省事写进文件。
API_ENV_ALLOWED_KEYS = frozenset(
    {
        "WORKPANDA_STORE_BACKEND",
        "WORKPANDA_SQLITE_PATH",
        "WORKPANDA_MODEL_BASE_URL",
        "WORKPANDA_MODEL_NAME",
        "WORKPANDA_MODEL_TIMEOUT_SECONDS",
        "WORKPANDA_MODEL_TRUST_ENV",
        "WORKPANDA_HARNESS_ENABLED",
        "WORKPANDA_HARNESS_MAX_STEPS",
        "WORKPANDA_HARNESS_MAX_TOOL_CALLS",
        "WORKPANDA_HARNESS_DEADLINE_SECONDS",
        "WORKPANDA_HARNESS_ALLOWED_HOSTS",
        "WORKPANDA_ENABLE_DEMO_FLOW",
        "WORKPANDA_RATE_LIMIT_PER_MINUTE",
        "WORKPANDA_HARNESS_AUDIT_PATH",
        "WORKPANDA_OCTOP_ENABLED",
        "WORKPANDA_OCTOP_PROVIDER",
        "WORKPANDA_OCTOP_BASE_URL",
        "WORKPANDA_OCTOP_MODEL",
        "WORKPANDA_OCTOP_WORKSPACE",
        "WORKPANDA_OCTOP_TIMEOUT_SECONDS",
    }
)

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def repo_root() -> Path:
    """部署工具所在的仓库根目录（deploy/ 的上一级）。"""
    return Path(__file__).resolve().parents[1]


def default_root() -> Path:
    return Path.home() / "Library" / "Application Support" / "WorkPanda"


def ensure_loopback(host: str) -> str:
    """只接受回环地址。传入 ``0.0.0.0`` 之类直接失败，而不是悄悄放行。"""
    normalized = (host or "").strip().lower()
    if normalized not in LOOPBACK_HOSTS:
        raise ValueError(
            f"拒绝监听 {host!r}：本工具只允许回环地址（对外暴露属于安全边界变更）"
        )
    return normalized


@dataclass(frozen=True)
class Layout:
    """持久化目录布局。所有可写状态都在 root 下，仓库里不留运行时产物。"""

    root: Path

    @classmethod
    def under(cls, root: Path) -> Layout:
        return cls(root=Path(root).expanduser())

    @property
    def api_venv(self) -> Path:
        return self.root / "api-venv"

    @property
    def octop_venv(self) -> Path:
        return self.root / "octop-venv"

    @property
    def octop_home(self) -> Path:
        return self.root / "octop-home"

    @property
    def data(self) -> Path:
        return self.root / "data"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def bin(self) -> Path:
        return self.root / "bin"

    @property
    def api_env(self) -> Path:
        return self.root / "api.env"

    @property
    def sqlite_path(self) -> Path:
        return self.data / "workpanda.db"

    @property
    def api_wrapper(self) -> Path:
        return self.bin / "run-api.sh"

    @property
    def octop_wrapper(self) -> Path:
        return self.bin / "run-octop.sh"

    def ensure_dirs(self) -> None:
        for path in (self.root, self.data, self.logs, self.bin, self.octop_home):
            path.mkdir(parents=True, exist_ok=True)
            path.chmod(0o700)


# ---------------------------------------------------------------------------
# 纯函数：包装脚本与 plist 渲染
# ---------------------------------------------------------------------------


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def _keychain_block(names: tuple[str, ...]) -> str:
    """生成「从钥匙串取变量」的 shell 片段。

    取不到就跳过并写日志：让服务以缺少该能力的状态起来（例如运维接口整体 404），
    而不是带着空字符串启动后假装配置成功。
    """
    lines = [
        f"KEYCHAIN_SERVICE={_shell_quote(KEYCHAIN_SERVICE)}",
        f"for NAME in {' '.join(names)}; do",
        "  VALUE=$("
        f'"{KEYCHAIN_TOOL}" find-generic-password -s "$KEYCHAIN_SERVICE" -a "$NAME" -w '
        "2>/dev/null || true)",
        '  if [ -n "$VALUE" ]; then',
        '    export "$NAME=$VALUE"',
        "  else",
        '    echo "keychain: $NAME 未配置，跳过" >&2',
        "  fi",
        "done",
        "unset VALUE",
    ]
    return "\n".join(lines)


def render_api_wrapper(layout: Layout, *, host: str = BIND_HOST, port: int = API_PORT) -> str:
    """WorkPanda 服务的启动脚本：先读非敏感配置，再从钥匙串补凭据。"""
    bind_host = ensure_loopback(host)
    return f"""#!/bin/sh
# 由 deploy/workpanda_deploy.py 生成，请勿手工编辑。
# 凭据不落盘：每次启动时从登录钥匙串读取。
set -eu
ROOT={_shell_quote(str(layout.root))}
[ -r "$ROOT/api.env" ] && . "$ROOT/api.env"
{_keychain_block(API_SECRET_NAMES)}
exec "$ROOT/api-venv/bin/python" -m uvicorn workpanda.main:app \\
  --host {bind_host} --port {port}
"""


def render_octop_wrapper(layout: Layout, *, host: str = BIND_HOST, port: int = OCTOP_PORT) -> str:
    """Octop 平台的启动脚本：HOME 指到持久目录，凭证同样走钥匙串。

    顺序有讲究：**先去钥匙串取凭据，再改 HOME**。``security`` 是按当前 HOME 下的
    ``Library/Keychains`` 找登录钥匙串的，先把 HOME 指到 octop-home 再取，就会
    「找不到条目」并把运维令牌静默丢掉（实测踩过：插件工具全部报缺凭据）。
    """
    bind_host = ensure_loopback(host)
    octop_home = _shell_quote(str(layout.octop_home))
    return f"""#!/bin/sh
# 由 deploy/workpanda_deploy.py 生成，请勿手工编辑。
# HOME 指向持久数据目录，.octop 随机器重启保留。
set -eu
ROOT={_shell_quote(str(layout.root))}
{_keychain_block(OCTOP_SECRET_NAMES)}
HOME={octop_home}
export HOME
OCTOP_HOME="$HOME/.octop"
export OCTOP_HOME
WORKPANDA_OPS_BASE_URL={_shell_quote(f"http://{bind_host}:{API_PORT}")}
export WORKPANDA_OPS_BASE_URL
LANGFUSE_TRACING_ENABLED=false
export LANGFUSE_TRACING_ENABLED
exec "$ROOT/octop-venv/bin/octop" run --host {bind_host} --port {port}
"""


def api_env_lines(layout: Layout, overrides: dict[str, str] | None = None) -> list[str]:
    """api.env 的内容：仅非敏感配置。含敏感键直接报错。"""
    defaults = {
        "WORKPANDA_STORE_BACKEND": "sqlite",
        "WORKPANDA_SQLITE_PATH": str(layout.sqlite_path),
        "WORKPANDA_ENABLE_DEMO_FLOW": "true",
    }
    merged = {**defaults, **(overrides or {})}
    rejected = sorted(key for key in merged if key not in API_ENV_ALLOWED_KEYS)
    if rejected:
        raise ValueError(f"api.env 只放非敏感配置，拒绝写入：{', '.join(rejected)}")
    return [f"export {key}={_shell_quote(value)}" for key, value in sorted(merged.items())]


def _agent_plist(
    *,
    label: str,
    program_arguments: list[str],
    environment: dict[str, str],
    log_path: Path,
    working_directory: Path,
) -> dict:
    return {
        "Label": label,
        "ProgramArguments": list(program_arguments),
        "EnvironmentVariables": dict(environment),
        "WorkingDirectory": str(working_directory),
        # 重启机器后自动起；进程异常退出由 launchd 拉起。
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "ProcessType": "Background",
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
    }


def api_plist(layout: Layout) -> dict:
    return _agent_plist(
        label=API_LABEL,
        program_arguments=[str(layout.api_wrapper)],
        # 敏感值一律走钥匙串，plist 里只有路径类配置。
        environment={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "PYTHONUNBUFFERED": "1"},
        log_path=layout.logs / "api.log",
        working_directory=layout.root,
    )


def octop_plist(layout: Layout) -> dict:
    return _agent_plist(
        label=OCTOP_LABEL,
        program_arguments=[str(layout.octop_wrapper)],
        environment={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "PYTHONUNBUFFERED": "1"},
        log_path=layout.logs / "octop.log",
        working_directory=layout.root,
    )


def plist_bytes(payload: dict) -> bytes:
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=False)


# ---------------------------------------------------------------------------
# 副作用薄封装：文件、钥匙串、launchctl、venv
# ---------------------------------------------------------------------------


def migrate_octop_agent_paths(db_path: Path, old_home: Path, new_home: Path) -> int:
    """把 Octop agent 配置里的旧 HOME 前缀换成新的持久目录。

    只改 ``agents.config_json`` 里的绝对路径（工作区、技能目录）。不换的话，
    agent 仍然指向 ``/tmp/...``，临时目录一清就找不到工作区 —— 这恰恰是本
    Issue 要消除的失效模式。必须在 Octop 停止时执行。
    """
    if not db_path.is_file():
        return 0
    prefixes = []
    for candidate in (Path(old_home), Path(old_home).resolve()):
        text = str(candidate)
        if text not in prefixes:
            prefixes.append(text)
    target = str(Path(new_home))
    changed = 0
    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute("select id, config_json from agents").fetchall()
        for agent_id, config_json in rows:
            if not config_json:
                continue
            updated = config_json
            for prefix in prefixes:
                updated = updated.replace(prefix, target)
            if updated != config_json:
                connection.execute(
                    "update agents set config_json = ? where id = ?", (updated, agent_id)
                )
                changed += 1
        connection.commit()
    finally:
        connection.close()
    return changed


def write_private_file(path: Path, content: str, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.chmod(mode)
    tmp.replace(path)


def launch_agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def keychain_set(name: str, value: str) -> None:
    """写入/更新登录钥匙串条目。

    ``security`` 的 ``-w`` 只在给了值时才写非空口令（不带值时从终端读，管道喂进去会
    存成空串），所以值只能走 argv：它在本机进程列表里出现几毫秒，且仅限显式的
    安装/轮换动作。文件里任何时刻都不会有明文。
    """
    if not value:
        raise ValueError("凭据值不能为空")
    # -T 只信任 security 自身；包装脚本正是用它读取，避免弹出授权框卡住 launchd 启动。
    subprocess.run(
        [
            KEYCHAIN_TOOL,
            "add-generic-password",
            "-U",
            "-s",
            KEYCHAIN_SERVICE,
            "-a",
            name,
            "-T",
            KEYCHAIN_TOOL,
            "-w",
            value,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )


def keychain_get(name: str) -> str | None:
    completed = subprocess.run(
        [KEYCHAIN_TOOL, "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", name, "-w"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def keychain_delete(name: str) -> None:
    subprocess.run(
        [KEYCHAIN_TOOL, "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", name],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def which_uv() -> str | None:
    return shutil.which("uv")


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=True, text=True, **kwargs)


def ensure_venv(python_bin: str, venv_dir: Path, *, rebuild: bool = False) -> None:
    uv = which_uv()
    if rebuild and venv_dir.exists():
        shutil.rmtree(venv_dir)
    if (venv_dir / "bin" / "python").exists():
        return
    if uv:
        _run([uv, "venv", "--python", python_bin, str(venv_dir)])
    else:
        _run([python_bin, "-m", "venv", str(venv_dir)])


def pip_install(
    venv_dir: Path, *requirements: str, find_links: tuple[str, ...] = ()
) -> None:
    """安装依赖。``find_links`` 用于离线/内网环境里的本地 wheel 目录。"""
    uv = which_uv()
    python = str(venv_dir / "bin" / "python")
    links = [arg for path in find_links for arg in ("--find-links", path)]
    if uv:
        _run([uv, "pip", "install", "--python", python, *links, *requirements])
    else:
        _run([python, "-m", "pip", "install", *links, *requirements])


def bootstrap_agent(plist_path: Path, label: str) -> None:
    domain = f"gui/{os.getuid()}"
    subprocess.run(
        ["launchctl", "bootout", f"{domain}/{label}"],
        capture_output=True,
        text=True,
        check=False,
    )
    _run(["launchctl", "bootstrap", domain, str(plist_path)])


def kickstart(label: str) -> None:
    subprocess.run(
        ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"],
        capture_output=True,
        text=True,
        check=False,
    )


def agent_state(label: str) -> str:
    completed = subprocess.run(
        ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return "missing"
    for line in completed.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("state ="):
            return stripped.split("=", 1)[1].strip()
    return "unknown"


# ---------------------------------------------------------------------------
# 探针
# ---------------------------------------------------------------------------


def http_status(url: str, *, headers: dict[str, str] | None = None) -> int:
    request = urllib.request.Request(url, headers=headers or {}, method="GET")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0


def wait_for(url: str, *, expected: int = 200, timeout: float = 40.0) -> int:
    deadline = time.monotonic() + timeout
    status = http_status(url)
    while status != expected and time.monotonic() < deadline:
        time.sleep(1.0)
        status = http_status(url)
    return status


def collect_status(layout: Layout) -> dict:
    ops_token = keychain_get("WORKPANDA_OPS_TOKEN")
    ops_headers = {"X-WorkPanda-Ops-Token": ops_token} if ops_token else {}
    return {
        "root": str(layout.root),
        "agents": {API_LABEL: agent_state(API_LABEL), OCTOP_LABEL: agent_state(OCTOP_LABEL)},
        "api_healthz": http_status(f"http://{BIND_HOST}:{API_PORT}/healthz"),
        "api_ops": http_status(
            f"http://{BIND_HOST}:{API_PORT}/api/ops/tasks", headers=ops_headers
        ),
        "octop_health": http_status(f"http://{BIND_HOST}:{OCTOP_PORT}/api/health"),
        "octop_plugins_anonymous": http_status(f"http://{BIND_HOST}:{OCTOP_PORT}/api/plugins"),
        "keychain": {name: keychain_get(name) is not None for name in ALL_SECRET_NAMES},
    }


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------


def cmd_install(args: argparse.Namespace) -> int:
    layout = Layout.under(Path(args.root))
    ensure_loopback(args.host)
    layout.ensure_dirs()

    if args.from_octop_home:
        source = Path(args.from_octop_home).expanduser() / ".octop"
        if not source.is_dir():
            raise SystemExit(f"找不到 Octop 数据目录：{source}")
        target = layout.octop_home / ".octop"
        if target.exists() and not args.force_migrate:
            print(f"目标已存在，跳过迁移：{target}", file=sys.stderr)
        else:
            # 旧 WAL/SHM 必须清掉：copytree 是合并语义，残留的旁路文件会和
            # 新拷入的 octop.db 对不上，SQLite 会报损坏。
            for stale in ("octop.db-wal", "octop.db-shm"):
                (target / stale).unlink(missing_ok=True)
            shutil.copytree(source, target, dirs_exist_ok=True)
            print(f"已迁移 Octop 数据目录：{source} -> {target}")
        try:
            rewritten = migrate_octop_agent_paths(
                target / "octop.db", Path(args.from_octop_home).expanduser(), layout.octop_home
            )
        except sqlite3.Error as exc:
            print(f"改写 agent 路径失败（Octop 可能仍在运行）：{exc}", file=sys.stderr)
        else:
            if rewritten:
                print(f"已改写 {rewritten} 个 agent 配置里的旧 HOME 前缀")

    if args.from_sqlite:
        source_db = Path(args.from_sqlite).expanduser()
        if not source_db.is_file():
            raise SystemExit(f"找不到 SQLite 文件：{source_db}")
        if layout.sqlite_path.exists() and not args.force_migrate:
            print(f"目标已存在，跳过迁移：{layout.sqlite_path}", file=sys.stderr)
        else:
            shutil.copy2(source_db, layout.sqlite_path)
            print(f"已迁移任务库：{source_db} -> {layout.sqlite_path}")

    find_links = tuple(args.find_links or ())
    ensure_venv(sys.executable, layout.api_venv, rebuild=args.rebuild)
    pip_install(layout.api_venv, args.repo, find_links=find_links)
    ensure_venv(sys.executable, layout.octop_venv, rebuild=args.rebuild)
    pip_install(layout.octop_venv, f"octop=={args.octop_version}", find_links=find_links)

    overrides = {}
    for item in args.set_env or []:
        key, _, value = item.partition("=")
        overrides[key] = value
    write_private_file(layout.api_env, "\n".join(api_env_lines(layout, overrides)) + "\n")
    write_private_file(layout.api_wrapper, render_api_wrapper(layout, host=args.host), mode=0o700)
    write_private_file(
        layout.octop_wrapper, render_octop_wrapper(layout, host=args.host), mode=0o700
    )

    launch_agents = launch_agents_dir()
    launch_agents.mkdir(parents=True, exist_ok=True)
    api_path = launch_agents / f"{API_LABEL}.plist"
    octop_path = launch_agents / f"{OCTOP_LABEL}.plist"
    api_path.write_bytes(plist_bytes(api_plist(layout)))
    octop_path.write_bytes(plist_bytes(octop_plist(layout)))
    print(f"已写入 launchd 配置：{api_path}、{octop_path}")

    if args.no_start:
        print("按 --no-start 跳过 launchd 引导")
        return 0

    # Octop 起得慢（加载 agent、技能、调度器，实测约 90 秒），先把 API 拉起来再等它。
    bootstrap_agent(api_path, API_LABEL)
    bootstrap_agent(octop_path, OCTOP_LABEL)
    wait_for(f"http://{BIND_HOST}:{API_PORT}/healthz", timeout=60.0)
    wait_for(f"http://{BIND_HOST}:{OCTOP_PORT}/api/health", timeout=180.0)
    status = collect_status(layout)
    print(json.dumps(status, ensure_ascii=False, indent=2))
    if status["api_healthz"] != 200 or status["octop_health"] != 200:
        print("服务未在预期时间内就绪，请查看 logs/ 下的日志", file=sys.stderr)
        return 1
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    layout = Layout.under(Path(args.root))
    # 刚 bootstrap 完端口还没绑上，直接探会得到一堆 0；给服务一点时间。
    if args.wait > 0:
        wait_for(f"http://{BIND_HOST}:{API_PORT}/healthz", timeout=args.wait)
        wait_for(f"http://{BIND_HOST}:{OCTOP_PORT}/api/health", timeout=args.wait)
    status = collect_status(layout)
    print(json.dumps(status, ensure_ascii=False, indent=2))
    healthy = status["api_healthz"] == 200 and status["octop_health"] == 200
    return 0 if healthy else 1


def cmd_secret(args: argparse.Namespace) -> int:
    if args.action == "list":
        for name in ALL_SECRET_NAMES:
            print(f"{name}: {'已配置' if keychain_get(name) else '缺失'}")
        return 0
    value = sys.stdin.read()
    value = value.rstrip("\n")
    if not value:
        print(
            "从标准输入读不到凭据值；用法：printf '%s' \"$VALUE\" | ... secret set NAME",
            file=sys.stderr,
        )
        return 2
    keychain_set(args.name, value)
    print(f"{args.name}: 已写入登录钥匙串（service={KEYCHAIN_SERVICE}）")
    return 0


def cmd_plugin(args: argparse.Namespace) -> int:
    layout = Layout.under(Path(args.root))
    plugin_dir = Path(args.plugin_dir).expanduser()
    if not (plugin_dir / "plugin.yaml").is_file():
        raise SystemExit(f"插件目录不合法（缺 plugin.yaml）：{plugin_dir}")
    octop_bin = layout.octop_venv / "bin" / "octop"
    if not octop_bin.exists():
        raise SystemExit(f"Octop 未安装：{octop_bin}")
    environment = {
        "HOME": str(layout.octop_home),
        "OCTOP_HOME": str(layout.octop_home / ".octop"),
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
    }
    # 1.0.2 的 plugin install 只接受目录或 URL，不接受本地 zip 路径。
    token = keychain_get("WORKPANDA_OPS_TOKEN")
    if token:
        environment["WORKPANDA_OPS_TOKEN"] = token
    environment["WORKPANDA_OPS_BASE_URL"] = f"http://{BIND_HOST}:{API_PORT}"
    _run(
        [str(octop_bin), "plugin", "install", str(plugin_dir), "--force"],
        env=environment,
        capture_output=False,
    )
    # 离线 reload 只重读磁盘；要作用到运行中的进程，还得让 launchd 重启。
    _run([str(octop_bin), "plugin", "reload"], env=environment)
    if not args.no_restart:
        kickstart(OCTOP_LABEL)
        wait_for(f"http://{BIND_HOST}:{OCTOP_PORT}/api/health", timeout=60.0)
    print("插件已重新安装并生效；控制台插件页状态需登录后确认")
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    layout = Layout.under(Path(args.root))
    domain = f"gui/{os.getuid()}"
    for label in (API_LABEL, OCTOP_LABEL):
        subprocess.run(
            ["launchctl", "bootout", f"{domain}/{label}"], capture_output=True, check=False
        )
        plist = launch_agents_dir() / f"{label}.plist"
        if plist.exists():
            plist.unlink()
    if args.purge_data:
        shutil.rmtree(layout.root, ignore_errors=True)
        print(f"已删除状态目录：{layout.root}")
    print("已卸载 launchd 服务（钥匙串条目保留，如需清理请手动删除）")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="workpanda_deploy",
        description="WorkPanda 试点持久部署：launchd 托管 + 登录钥匙串管理凭据",
    )
    parser.add_argument(
        "--root",
        default=str(default_root()),
        help="状态目录（默认 ~/Library/Application Support/WorkPanda）",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    install = subparsers.add_parser("install", help="创建 venv、写配置与 launchd 单元并启动")
    install.add_argument("--repo", default=str(repo_root()), help="WorkPanda 仓库路径")
    install.add_argument("--host", default=BIND_HOST, help="监听地址，仅允许回环")
    install.add_argument("--octop-version", default=DEFAULT_OCTOP_VERSION)
    install.add_argument(
        "--find-links",
        action="append",
        metavar="DIR",
        help="本地 wheel 目录（内网/离线安装时用，可重复）",
    )
    install.add_argument("--rebuild", action="store_true", help="重建 venv")
    install.add_argument("--no-start", action="store_true", help="只落盘不引导 launchd")
    install.add_argument("--from-octop-home", default=None, help="从旧 HOME 迁移 .octop 数据目录")
    install.add_argument("--from-sqlite", default=None, help="从旧路径迁移任务库")
    install.add_argument("--force-migrate", action="store_true", help="覆盖已存在的目标数据")
    install.add_argument(
        "--set-env",
        action="append",
        metavar="KEY=VALUE",
        help="覆盖 api.env 中的非敏感配置（可重复）",
    )
    install.set_defaults(func=cmd_install)

    status = subparsers.add_parser("status", help="检查 launchd 状态与端口探针")
    status.add_argument(
        "--wait", type=float, default=15.0, help="每个探针最多等待多少秒（0 表示不等待）"
    )
    status.set_defaults(func=cmd_status)

    secret = subparsers.add_parser("secret", help="管理登录钥匙串中的凭据")
    secret.add_argument("action", choices=["set", "list"])
    secret.add_argument("name", nargs="?", default="", help="凭据名，如 WORKPANDA_OPS_TOKEN")
    secret.set_defaults(func=cmd_secret)

    plugin = subparsers.add_parser("plugin", help="按目录重装 Octop 插件并重载")
    plugin.add_argument(
        "--plugin-dir",
        default=str(repo_root() / "plugins" / "workpanda-ops"),
        help="插件目录（含 plugin.yaml）",
    )
    plugin.add_argument("--no-restart", action="store_true", help="不重启 Octop")
    plugin.set_defaults(func=cmd_plugin)

    uninstall = subparsers.add_parser("uninstall", help="停止并移除 launchd 服务")
    uninstall.add_argument("--purge-data", action="store_true", help="同时删除状态目录")
    uninstall.set_defaults(func=cmd_uninstall)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "secret" and args.action == "set" and not args.name:
        parser.error("secret set 需要凭据名")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
