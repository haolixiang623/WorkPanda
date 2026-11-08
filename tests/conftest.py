"""测试夹具：使用真实流程定义 + 可控执行器，不依赖外部系统。"""

import shutil
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest

from tests.support import TEST_FLOWS
from workpanda.bootstrap import Container, build_container
from workpanda.config import Settings
from workpanda.infrastructure.adapters import KeywordIntentRouter, ScriptedExecutor

# 测试进程永不读开发者本地的 `.env`（#72）。
#
# `Settings` 的 `env_file=".env"` 是相对 cwd 解析的，本机一旦存在 `.env`，
# WORKPANDA_OPS_TOKEN / WORKPANDA_STORE_BACKEND / WORKPANDA_MODEL_TIMEOUT_SECONDS
# 之类就会注入进来，而多条用例是**依赖代码默认值**的，于是出现「本地红、CI 绿」：
# worker 分不清「我改坏了」和「环境噪声」，还会诱发「为了变绿去改断言」。
#
# 只改测试进程内的这份类配置；`config.py` 不动，应用运行时照常读 `.env`。
# 不要删这一行，也不要改成依赖「记得先在 shell 里 unset」——那正是本 issue 要
# 消灭的口头约定。若个别用例需要显式环境值，直接给 `Settings(...)` 传参。
Settings.model_config["env_file"] = None


@pytest.fixture
def workdir() -> Iterator[Path]:
    """在仓库内创建临时目录，避免依赖系统临时目录的权限与清理策略。"""

    root = Path(".pytest-tmp") / uuid4().hex
    root.mkdir(parents=True, exist_ok=True)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def executor() -> ScriptedExecutor:
    return ScriptedExecutor()


@pytest.fixture
def container(executor: ScriptedExecutor) -> Iterator[Container]:
    built = build_container(
        Settings(store_backend="memory", enable_demo_flow=False),
        flows=TEST_FLOWS,
        executor=executor,
        router=KeywordIntentRouter(),
    )
    yield built
    built.close()
