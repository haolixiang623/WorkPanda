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
