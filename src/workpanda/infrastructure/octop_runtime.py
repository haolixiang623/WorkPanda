"""Octop-Harness 运行时适配器。

边界：
- Octop 只负责"理解与执行单个受控步骤"，业务流程、状态、权限和确认仍由本服务持有；
- 适配器把执行结果映射为 succeeded / failed / unconfirmed 三态；
  超时和无法判定的异常必须映射为 unconfirmed，绝不能报成功。

`HarnessGateway` 是同步端口，便于在现有同步流程推进器里使用；
真实实现通过后台事件循环线程调用 Octop 的异步 API。
"""

import asyncio
import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from workpanda.application.ports import (
    ExecutionOutcome,
    ExecutionRequest,
    ExecutionResult,
    Executor,
)

logger = logging.getLogger(__name__)

DEFAULT_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com",
}


class HarnessUnavailable(RuntimeError):
    """Octop 运行时不可用或未安装。"""


class HarnessGateway(Protocol):
    def run(self, *, prompt: str, thread_id: str, timeout_seconds: float) -> dict[str, Any]:
        """执行一次受控调用，返回结构化的模型输出。"""
        ...

    def close(self) -> None: ...


class OctopHarnessGateway:
    """基于 octop-harness SDK 的实现。

    SDK 是异步 API，这里用独立事件循环线程承载，避免与 Web 事件循环互相阻塞。
    """

    def __init__(
        self,
        *,
        agent_name: str,
        model: str,
        provider: str = "openai",
        api_key: str | None = None,
        base_url: str | None = None,
        workspace: str | None = None,
    ) -> None:
        self._agent_name = agent_name
        self._model = model
        self._provider = provider
        self._api_key = api_key
        self._base_url = base_url
        self._workspace = workspace
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop, name="octop-harness-loop", daemon=True
        )
        self._thread.start()
        self._agent: Any = None
        self._manager: Any = None

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _ensure_agent(self) -> Any:
        if self._agent is not None:
            return self._agent
        try:
            from octop_harness import (
                HarnessAgentConfig,
                HarnessAgentManager,
                ModelConfig,
                ProviderConfig,
            )
        except ImportError as exc:  # pragma: no cover - 依赖未安装时才会触发
            raise HarnessUnavailable(
                "octop-harness 未安装，请执行 pip install 'workpanda[octop]'"
            ) from exc

        if not self._api_key:
            raise HarnessUnavailable("缺少模型凭据：请配置 WORKPANDA_OCTOP_API_KEY")

        provider = ProviderConfig(
            id=self._provider,
            name=self._provider,
            base_url=self._base_url or DEFAULT_BASE_URLS.get(self._provider, ""),
            api_key=self._api_key,
            models=[ModelConfig(id=self._model)],
        )
        config = HarnessAgentConfig(
            name=self._agent_name,
            workspace_dir=str(Path(self._workspace or ".octop/workpanda").expanduser().resolve()),
            providers=[provider],
            default_model=f"{provider.id}/{self._model}",
            # 工具与沙箱由本服务的流程编排决定，这里不再叠加额外自动化能力。
            todos_enabled=False,
        )
        self._manager = HarnessAgentManager()
        self._agent = self._manager.create_agent(config).agent
        return self._agent

    def run(self, *, prompt: str, thread_id: str, timeout_seconds: float) -> dict[str, Any]:
        agent = self._ensure_agent()
        from octop_harness import ChatRequest

        request = ChatRequest(messages=prompt, thread_id=thread_id, source="workpanda")
        future = asyncio.run_coroutine_threadsafe(agent.call(request), self._loop)
        result = future.result(timeout=timeout_seconds)
        if not isinstance(result, dict):
            return {"raw": result}
        return result

    def close(self) -> None:
        try:
            if self._manager is not None:
                self._manager.close()
        except Exception:  # pragma: no cover - 关闭失败不应影响进程退出
            logger.warning("failed to close Octop harness manager", exc_info=True)
        finally:
            if self._loop.is_running():
                self._loop.call_soon_threadsafe(self._loop.stop)


class OctopHarnessExecutor(Executor):
    """把流程执行请求转成一次 Octop 受控调用。"""

    def __init__(
        self,
        gateway: HarnessGateway,
        *,
        timeout_seconds: float = 30.0,
        prompt_builder: Callable[[ExecutionRequest], str] | None = None,
    ) -> None:
        self._gateway = gateway
        self._timeout_seconds = timeout_seconds
        self._prompt_builder = prompt_builder or default_prompt

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        prompt = self._prompt_builder(request)
        try:
            output = self._gateway.run(
                prompt=prompt,
                thread_id=str(request.task_id),
                timeout_seconds=self._timeout_seconds,
            )
        except TimeoutError:
            # 已发出但结果未知，不能视为失败后重试。
            return ExecutionResult(
                outcome=ExecutionOutcome.UNCONFIRMED,
                error_code="runtime_timeout",
                message="执行未在预期时间内返回结果，状态待确认。",
            )
        except HarnessUnavailable as exc:
            return ExecutionResult(
                outcome=ExecutionOutcome.FAILED,
                error_code="runtime_unavailable",
                message=str(exc),
            )
        except Exception as exc:  # noqa: BLE001 - 外部运行时异常必须转成受控结果
            logger.warning("octop harness call failed", exc_info=True)
            return ExecutionResult(
                outcome=ExecutionOutcome.UNCONFIRMED,
                error_code="runtime_error",
                message=f"执行异常，结果无法确认：{type(exc).__name__}",
            )

        return ExecutionResult(
            outcome=ExecutionOutcome.SUCCEEDED,
            output={"tool": request.tool, "step_id": request.step_id, "runtime": output},
        )


def default_prompt(request: ExecutionRequest) -> str:
    """只把当前步骤需要的信息交给运行时，避免整体流程与无关数据外泄。"""

    arguments = ", ".join(f"{key}={value}" for key, value in sorted(request.arguments.items()))
    return (
        f"流程 {request.flow_id}@{request.flow_version} 的步骤 {request.step_id}，"
        f"工具 {request.tool}。参数：{arguments or '无'}。"
        "请只执行该步骤并获得结果，不要自行改变流程或调用其他工具。"
    )
