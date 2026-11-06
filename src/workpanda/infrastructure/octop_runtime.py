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

# 结果契约属于通用 harness，不属于任何厂商适配器；此处只做复用与再导出。
from workpanda.harness.contract import OUTCOME_CONTRACT, parse_contract

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

        workspace = Path(self._workspace or ".octop/workpanda").expanduser().resolve()
        # 预先创建技能目录，避免运行时反复提示路径不存在。
        (workspace / "skills").mkdir(parents=True, exist_ok=True)

        provider = ProviderConfig(
            id=self._provider,
            name=self._provider,
            base_url=self._base_url or DEFAULT_BASE_URLS.get(self._provider, ""),
            api_key=self._api_key,
            models=[ModelConfig(id=self._model)],
        )
        config = HarnessAgentConfig(
            name=self._agent_name,
            workspace_dir=str(workspace),
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
        return normalize_result(result)

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

        # 网关可能返回原始 SDK 结构（含 messages），也可能是已归一化的文本；
        # 两种都要能处理，否则会把"有回复"误读成"空回复"。
        normalized = _as_normalized(output)
        text = str(normalized.get("text", ""))
        outcome, detail = parse_contract(text)
        succeeded = outcome is ExecutionOutcome.SUCCEEDED
        return ExecutionResult(
            outcome=outcome,
            output={
                "tool": request.tool,
                "step_id": request.step_id,
                "text": text[:2000],
                "verification": detail,
                "message_count": normalized.get("message_count", 0),
            },
            # 未成功时错误码必须可读：优先用具体原因，否则退回三态本身。
            error_code=None if succeeded else str(detail.get("reason") or outcome.value),
            message=None if succeeded else str(detail.get("summary") or "模型未给出可核验结果"),
        )


def _as_normalized(output: Any) -> dict[str, Any]:
    """统一网关返回形状，便于后续取值。"""

    if isinstance(output, dict) and isinstance(output.get("messages"), list):
        return normalize_result(output)
    if isinstance(output, dict):
        return output
    return normalize_result(output)


def normalize_result(result: Any) -> dict[str, Any]:
    """把 SDK 返回归一化成可持久化的 JSON。

    Octop 的 `call` 返回 LangChain 消息对象，不能直接 json 序列化，
    这里只保留最后一条助手回复文本和消息条数，避免把内部对象写进数据库。
    """

    if not isinstance(result, dict):
        return {"text": str(result)[:4000], "message_count": 0}

    messages = result.get("messages")
    if not isinstance(messages, list):
        return {"text": str(result.get("content") or result)[:4000], "message_count": 0}

    text = ""
    for message in reversed(messages):
        if getattr(message, "type", "") == "ai" or type(message).__name__ == "AIMessage":
            content = getattr(message, "content", "")
            text = content if isinstance(content, str) else str(content)
            break
    return {"text": text[:4000], "message_count": len(messages)}


def default_prompt(request: ExecutionRequest) -> str:
    """只把当前步骤需要的信息交给运行时，避免整体流程与无关数据外泄。"""

    arguments = ", ".join(f"{key}={value}" for key, value in sorted(request.arguments.items()))
    instruction = request.instruction or "执行该步骤并如实报告结果。"
    return (
        f"任务步骤：{request.step_id}\n"
        f"执行要求：{instruction}\n"
        f"参数：{arguments or '无'}\n"
        # tool 是服务端为该步骤选定的能力标识，仅用于记录与审计；
        # 模型若把它当成"必须调用的工具"就会在找不到时误报失败。
        f"说明：{request.tool} 是本服务为该步骤选定的能力标识，仅用于服务端记录与审计，"
        "你不需要在运行时调用同名工具；请直接按执行要求完成本步骤。\n"
        f"{OUTCOME_CONTRACT}"
    )
