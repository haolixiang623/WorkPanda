"""测试进程与开发者本地 `.env` 的隔离（#72）。

这一条不是「测试测试框架」，而是把一条真实发生过的失效钉住：`Settings` 的
`env_file=".env"` 相对 cwd 解析，本机存在 `.env` 时会把 `WORKPANDA_STORE_BACKEND`
之类注入进来，于是依赖代码默认值的用例**本地红、CI 绿**，worker 分不清
「我改坏了」和「环境噪声」，并会诱发「为了变绿去改断言」。

隔离统一放在 `tests/conftest.py` 一处；本文件只负责在有人把那一行删掉时立刻变红。
"""

from __future__ import annotations

from pathlib import Path

from workpanda.config import Settings


def test_local_env_file_does_not_leak_into_settings(tmp_path: Path, monkeypatch) -> None:
    """cwd 里存在 `.env` 时，测试拿到的仍是代码默认值。"""

    (tmp_path / ".env").write_text(
        "WORKPANDA_STORE_BACKEND=sqlite\n"
        "WORKPANDA_MODEL_TIMEOUT_SECONDS=120\n"
        "WORKPANDA_OPS_TOKEN=boss:admin:local-dev-token-0123456789\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    settings = Settings()
    assert settings.store_backend == "memory"  # 默认值，不是 .env 里的 sqlite
    assert settings.model_timeout_seconds != 120  # .env 里的值没有生效
    assert settings.ops_token is None


def test_settings_model_config_no_longer_points_at_dot_env() -> None:
    """形状断言：屏蔽必须落在类配置上，而不是靠某个用例单独传 `_env_file=None`。"""

    assert Settings.model_config.get("env_file") is None
