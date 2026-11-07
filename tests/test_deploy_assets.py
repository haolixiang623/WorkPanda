"""持久化部署资产的自检。

放在仓库里的只有「怎么部署」，真正的凭据与状态都在机器上。因此这里守的是
四条不变量：

1. launchd 单元只含路径与端口，重启后自动拉起，且绝不含凭据；
2. 只监听回环地址 —— 没有任何把 bind_host 改成 ``0.0.0.0`` 或打开 TLS 的入口；
3. ``api.env`` 只接受白名单内的非敏感键，凭据只能进钥匙串；
4. 包装脚本每次启动从钥匙串取值，取不到就降级（跳过）而不是带空值启动。
"""
from __future__ import annotations

import importlib.util
import json
import plistlib
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "deploy" / "workpanda_deploy.py"
_spec = importlib.util.spec_from_file_location("workpanda_deploy", MODULE_PATH)
deploy = importlib.util.module_from_spec(_spec)
# dataclasses 会回查 sys.modules[cls.__module__]，动态加载必须先登记模块。
sys.modules[_spec.name] = deploy
_spec.loader.exec_module(deploy)


@pytest.fixture
def layout(tmp_path: Path) -> deploy.Layout:
    return deploy.Layout.under(tmp_path / "WorkPanda")


def test_layout_keeps_all_state_under_root(layout: deploy.Layout) -> None:
    for path in (
        layout.api_venv,
        layout.octop_venv,
        layout.octop_home,
        layout.data,
        layout.logs,
        layout.bin,
        layout.api_env,
        layout.sqlite_path,
        layout.api_wrapper,
        layout.octop_wrapper,
    ):
        assert layout.root in path.parents


def test_api_plist_is_persistent_and_secret_free(layout: deploy.Layout) -> None:
    payload = deploy.api_plist(layout)
    raw = deploy.plist_bytes(payload)
    parsed = plistlib.loads(raw)

    assert parsed["Label"] == "com.workpanda.api"
    assert parsed["ProgramArguments"] == [str(layout.api_wrapper)]
    # 机器重启自动起、异常退出由 launchd 拉起，这是「持久」的定义。
    assert parsed["RunAtLoad"] is True
    assert parsed["KeepAlive"] is True
    assert parsed["StandardOutPath"] == str(layout.logs / "api.log")

    environment = parsed["EnvironmentVariables"]
    for name in deploy.API_SECRET_NAMES:
        assert name not in environment
    assert b"0.0.0.0" not in raw
    assert b"tls" not in raw.lower()


def test_octop_plist_is_persistent_and_secret_free(layout: deploy.Layout) -> None:
    raw = deploy.plist_bytes(deploy.octop_plist(layout))
    parsed = plistlib.loads(raw)

    assert parsed["Label"] == "com.workpanda.octop"
    assert parsed["ProgramArguments"] == [str(layout.octop_wrapper)]
    assert parsed["RunAtLoad"] is True
    assert parsed["KeepAlive"] is True
    assert deploy.OCTOP_SECRET_NAMES[0] not in parsed["EnvironmentVariables"]
    assert b"0.0.0.0" not in raw


@pytest.mark.parametrize("renderer", ["render_api_wrapper", "render_octop_wrapper"])
def test_wrappers_pull_secrets_from_keychain_at_startup(
    layout: deploy.Layout, renderer: str
) -> None:
    script = getattr(deploy, renderer)(layout)

    assert script.startswith("#!/bin/sh")
    assert "set -eu" in script
    assert "find-generic-password" in script
    assert deploy.KEYCHAIN_SERVICE in script
    assert "exec " in script
    # 取不到就叫醒日志并跳过，绝不能带空值启动后假装配置成功。
    assert "未配置，跳过" in script


def test_api_wrapper_binds_loopback_and_fixed_port(layout: deploy.Layout) -> None:
    script = deploy.render_api_wrapper(layout)
    assert "--host 127.0.0.1 --port 8010" in script
    assert "0.0.0.0" not in script
    # 配置来自 api.env，凭据来自钥匙串，脚本本身不含任何值。
    assert ". \"$ROOT/api.env\"" in script


def test_octop_wrapper_reads_keychain_before_overriding_home(layout: deploy.Layout) -> None:
    # security 按 HOME 找登录钥匙串；先改 HOME 再取凭据会「找不到条目」，
    # 于是运维令牌被静默丢掉、插件工具全部报缺凭据（实测踩过一次）。
    script = deploy.render_octop_wrapper(layout)
    keychain_at = script.index("find-generic-password")
    home_at = script.index("HOME=" + deploy._shell_quote(str(layout.octop_home)))
    assert keychain_at < home_at


def test_octop_wrapper_uses_persistent_home(layout: deploy.Layout) -> None:
    script = deploy.render_octop_wrapper(layout)
    assert f"HOME={deploy._shell_quote(str(layout.octop_home))}" in script
    assert 'OCTOP_HOME="$HOME/.octop"' in script
    assert "octop-venv/bin/octop\" run --host 127.0.0.1 --port 8090" in script
    assert "WORKPANDA_OPS_BASE_URL='http://127.0.0.1:8010'" in script


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", " 127.0.0.1 "])
def test_loopback_hosts_accepted(host: str) -> None:
    assert deploy.ensure_loopback(host) in deploy.LOOPBACK_HOSTS


@pytest.mark.parametrize("host", ["0.0.0.0", "", "192.168.1.5", "example.com", "::"])
def test_public_bind_rejected(host: str) -> None:
    # 对外暴露属于安全边界变更，工具里连「试一下」的入口都不留。
    with pytest.raises(ValueError):
        deploy.ensure_loopback(host)


@pytest.mark.parametrize("name", ["WORKPANDA_OPS_TOKEN", "WORKPANDA_MODEL_API_KEY", "SECRET"])
def test_api_env_rejects_secret_keys(layout: deploy.Layout, name: str) -> None:
    with pytest.raises(ValueError):
        deploy.api_env_lines(layout, {name: "value"})


def test_api_env_defaults_point_at_persistent_sqlite(layout: deploy.Layout) -> None:
    lines = deploy.api_env_lines(layout)
    joined = "\n".join(lines)

    assert f"WORKPANDA_SQLITE_PATH='{layout.sqlite_path}'" in joined
    assert "WORKPANDA_STORE_BACKEND='sqlite'" in joined
    assert lines == sorted(lines)


def test_api_env_quotes_awkward_values(layout: deploy.Layout) -> None:
    lines = deploy.api_env_lines(layout, {"WORKPANDA_MODEL_BASE_URL": "http://h/a b'c"})
    line = next(item for item in lines if "WORKPANDA_MODEL_BASE_URL" in item)
    # 单引号必须转义成 '\''，否则 source 时会截断后面的内容。
    assert line == "export WORKPANDA_MODEL_BASE_URL='http://h/a b'\\''c'"


def test_write_private_file_sets_mode(tmp_path: Path) -> None:
    target = tmp_path / "bin" / "run-api.sh"
    deploy.write_private_file(target, "#!/bin/sh\n", mode=0o700)
    assert target.read_text(encoding="utf-8") == "#!/bin/sh\n"
    assert target.stat().st_mode & 0o777 == 0o700
    assert not target.with_suffix(".sh.tmp").exists()


def test_deploy_tree_has_no_credentials() -> None:
    # 与 CI 的敏感信息扫描同口径：文件本身必须是模板，不能是配置残留。
    pattern = re.compile(
        r"(AKIA[0-9A-Z]{16}|-----BEGIN (RSA|EC|OPENSSH|PRIVATE) KEY-----"
        r"|gh[pousr]_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9]{20,})"
    )
    for path in sorted((REPO_ROOT / "deploy").rglob("*")):
        if path.is_file():
            assert not pattern.search(path.read_text(encoding="utf-8")), path


def test_migrate_octop_agent_paths_rewrites_old_home(tmp_path: Path) -> None:
    import sqlite3

    db_path = tmp_path / "octop.db"
    connection = sqlite3.connect(db_path)
    connection.execute("create table agents (id text primary key, config_json text)")
    old_home = tmp_path / "old-home"
    connection.execute(
        "insert into agents values (?, ?)",
        ("A1", json.dumps({"workspace": str(old_home / ".octop" / "agents" / "A1")})),
    )
    connection.execute("insert into agents values (?, ?)", ("A2", None))
    connection.commit()
    connection.close()

    new_home = tmp_path / "new-home"
    changed = deploy.migrate_octop_agent_paths(db_path, old_home, new_home)

    connection = sqlite3.connect(db_path)
    stored = connection.execute("select config_json from agents where id = 'A1'").fetchone()[0]
    untouched = connection.execute("select config_json from agents where id = 'A2'").fetchone()
    assert untouched[0] is None
    connection.close()
    assert changed == 1
    assert str(old_home) not in stored
    assert str(new_home) in stored


def test_cli_help_runs() -> None:
    completed = subprocess.run(
        [sys.executable, str(MODULE_PATH), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0
    assert "install" in completed.stdout
