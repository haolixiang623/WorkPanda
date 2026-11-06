"""测试夹具：使用真实流程定义 + 可控执行器，不依赖外部系统。"""

from collections.abc import Iterator

import pytest

from tests.support import TEST_FLOWS
from workpanda.bootstrap import Container, build_container
from workpanda.config import Settings
from workpanda.infrastructure.adapters import KeywordIntentRouter, ScriptedExecutor


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
