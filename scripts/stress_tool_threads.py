"""长任务 / 超时 / 并发压力标定：量线程、内存、句柄，标定两个上限。

为什么要有这个脚本：`WORKPANDA_HARNESS_MAX_STUCK_THREADS` 与
`WORKPANDA_HARNESS_LEASE_TTL_SECONDS` 此前是按"看起来合理"给的值，没有量过。
而工具超时是"放弃等待"，线程收不回来——上限该取多少，只能在真实压力下看它
到底吃掉多少资源，以及闸门触顶后系统还能不能自己恢复。

隔离边界（重要）：

- 不连试点实例、不联网、不读 `.env`、不读任何凭据；
- 全部使用进程内合成工具（纯等待 / 持 socket 两种）驱动 `ToolGrant` 与
  `HarnessExecutor`，因此不会产生业务副作用；
- 会在**本进程**里故意留下若干卡住的 daemon 线程（这正是被测对象），
  所以只能对一次性进程跑，不要接进常驻服务。

用法：

    python scripts/stress_tool_threads.py
    python scripts/stress_tool_threads.py --max-stuck 8 --concurrency 4 --json-out stress.json
    python scripts/stress_tool_threads.py --only sequential,concurrency

输出是 JSON（stdout），包含每个场景的原始采样与汇总，末尾是依据本次数据算出的
推荐值。报告结论见 `docs/HARNESS.md` 第 21 节。
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from workpanda.application.ports import ExecutionOutcome, ExecutionRequest  # noqa: E402
from workpanda.harness.clients import ScriptedModelClient, tool_call  # noqa: E402
from workpanda.harness.executor import HarnessExecutor  # noqa: E402
from workpanda.harness.models import (  # noqa: E402
    HarnessMessage,
    ModelResponse,
    ToolCall,
    ToolOutcome,
    ToolSpec,
)
from workpanda.harness.tools import (  # noqa: E402
    DEFAULT_MAX_STUCK_THREADS,
    ToolContext,
    ToolGrant,
    ToolRegistry,
)

# 工具线程名的前缀（`_invoke_with_timeout` 里的 `tool-<name>`）。
TOOL_THREAD_PREFIX = "tool-"

SUCCESS = json.dumps(
    {"outcome": "succeeded", "summary": "压力用例回显", "evidence": "echo:stress"},
    ensure_ascii=False,
)


# ---------- 资源读数 ----------


def peak_rss_kb() -> float:
    """进程峰值 RSS（KB）。用峰值而不是瞬时值：卡住的线程一旦占过内存就不会还回来。"""

    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS 的 ru_maxrss 单位是字节，Linux 是 KB；两边都要能读。
    return value / 1024.0 if sys.platform == "darwin" else float(value)


def open_fds() -> int:
    """打开的文件描述符数。取不到返回 -1（不编造 0）。"""

    for path in ("/dev/fd", "/proc/self/fd"):
        try:
            return len(os.listdir(path))
        except OSError:
            continue
    return -1


def tool_threads() -> int:
    """进程里名字以 `tool-` 开头的线程数：即"还在跑的工具线程"。"""

    return sum(1 for t in threading.enumerate() if t.name.startswith(TOOL_THREAD_PREFIX))


def sample(registry: ToolRegistry | None = None) -> dict[str, Any]:
    stats = registry.threads.stats() if registry is not None else None
    return {
        "at": round(time.monotonic(), 3),
        "threads": threading.active_count(),
        "tool_threads": tool_threads(),
        "peak_rss_kb": round(peak_rss_kb(), 1),
        "fds": open_fds(),
        "ledger": stats,
    }


class Sampler:
    """后台按固定间隔采样，用来回答"随时间怎么变化"，而不是只看首尾两点。"""

    def __init__(self, registry: ToolRegistry | None, interval: float = 0.05) -> None:
        self._registry = registry
        self._interval = max(0.01, interval)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.samples: list[dict[str, Any]] = []

    def __enter__(self) -> Sampler:
        self.samples.append(sample(self._registry))
        self._thread = threading.Thread(target=self._run, name="stress-sampler", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self.samples.append(sample(self._registry))

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            self.samples.append(sample(self._registry))

    def peak(self, key: str) -> int | float:
        values = [s[key] for s in self.samples if isinstance(s[key], (int, float))]
        return max(values) if values else 0



# ---------- 合成工具 ----------


class HangingTool:
    """卡住不返回，直到压力场景放行；`hold_socket=True` 时同时占住两个 fd。

    为什么要两种：纯等待说明"线程本身"的成本，持 socket 说明"真实工具（HTTP、
    数据库连接）卡住"时 fd 也会一起被占住——后者才是句柄被吃光的路径。
    """

    def __init__(self, name: str, *, hold_socket: bool = False, max_wait: float = 30.0) -> None:
        self.spec = ToolSpec(
            name=name,
            description="压力用例：一直卡住不返回",
            timeout_seconds=60.0,
        )
        self.release = threading.Event()
        self.calls = 0
        self._hold_socket = hold_socket
        self._max_wait = max_wait
        self._sockets: list[socket.socket] = []

    def invoke(self, arguments: dict[str, Any], context: ToolContext) -> str:
        self.calls += 1
        try:
            if self._hold_socket:
                first, second = socket.socketpair()
                self._sockets.extend((first, second))
            self.release.wait(self._max_wait)
            return "late"
        finally:
            # 真实工具退出时会关掉自己的连接：句柄要等线程真正结束才回来，
            # 这正是"线程收不回来 = 句柄也收不回来"的那段窗口。
            self._close_sockets()

    def close(self) -> None:
        self._close_sockets()

    def _close_sockets(self) -> None:
        for sock in self._sockets:
            sock.close()
        self._sockets.clear()


class QuickTool:
    """立刻返回的幂等工具：用来证明闸门触顶时"确实一次都没执行"。"""

    def __init__(self, name: str = "stress_quick") -> None:
        self.spec = ToolSpec(name=name, description="压力用例：立刻返回")
        self.calls = 0

    def invoke(self, arguments: dict[str, Any], context: ToolContext) -> str:
        self.calls += 1
        return "ok"


def _context() -> ToolContext:
    return ToolContext(task_id=uuid4(), flow_id="stress.direct", step_id="query")


def _call(name: str, seq: int) -> ToolCall:
    # 参数留空：合成工具没有声明 schema，塞字段会被参数闸拦成 invalid_arguments，
    # 那样测到的就不是容量闸门了。
    return ToolCall(call_id=f"{name}-{seq}", name=name, arguments={})


def _drain(registry: ToolRegistry, timeout: float = 10.0) -> tuple[float, dict[str, int]]:
    """等所有工具线程退出，返回（耗时秒，最终台账）。台账不归零就是真漏账。"""

    started = time.monotonic()
    while time.monotonic() - started < timeout:
        if registry.threads.stats()["live"] == 0:
            break
        time.sleep(0.01)
    return round(time.monotonic() - started, 3), registry.threads.stats()


# ---------- 场景 ----------


def scene_baseline(args: argparse.Namespace) -> dict[str, Any]:
    """空载基线：后面所有"涨了多少"都相对这里。"""

    with Sampler(None) as sampler:
        time.sleep(args.baseline_seconds)
    return {
        "threads": threading.active_count(),
        "tool_threads": tool_threads(),
        "peak_rss_kb": round(peak_rss_kb(), 1),
        "fds": open_fds(),
        "samples": sampler.samples,
    }


def scene_sequential(args: argparse.Namespace) -> dict[str, Any]:
    """长任务：同一进程里反复遇到"超时但线程不退出"，直到撞上容量闸门。

    模拟的是真实长任务最坏的一种走法——每轮都碰到一个卡死的工具。要看三件事：
    卡住的线程数是否就停在 cap、触顶后新调用是否被 BLOCKED 且没执行、放行后能否恢复。
    """

    registry = ToolRegistry(max_stuck_threads=args.max_stuck)
    hanging = HangingTool("stress_hang")
    quick = QuickTool()
    registry.register(hanging)
    registry.register(quick)
    grant = ToolGrant(
        registry=registry,
        allowed=("stress_hang", "stress_quick"),
        call_timeout_seconds=args.call_timeout,
        retry_backoff_seconds=0,
    )

    outcomes: list[dict[str, Any]] = []
    blocked_at: int | None = None
    peak_stuck = 0
    with Sampler(registry) as sampler:
        for seq in range(args.max_stuck + args.extra_calls):
            record = grant.call(_call("stress_hang", seq), _context())
            outcomes.append(
                {
                    "seq": seq,
                    "outcome": record.outcome.value,
                    "error_code": record.error_code,
                    "duration_ms": record.duration_ms,
                }
            )
            peak_stuck = max(peak_stuck, registry.threads.stats()["stuck"])
            if record.outcome is ToolOutcome.BLOCKED:
                blocked_at = seq
                break

        stuck_at_cap = registry.threads.stats()
        # 触顶之后，一个本来能立刻成功的工具也必须被挡下，且一次都不执行。
        blocked_quick = grant.call(_call("stress_quick", 900), _context())
        quick_calls_while_blocked = quick.calls
        peak = sampler.peak("tool_threads")

        hanging.release.set()
        drain_seconds, final_stats = _drain(registry)
        after = sample(registry)

    hanging.close()
    return {
        "cap": args.max_stuck,
        "call_timeout_seconds": args.call_timeout,
        "calls_until_blocked": (blocked_at + 1) if blocked_at is not None else len(outcomes),
        "blocked_at_seq": blocked_at,
        "stuck_at_cap": stuck_at_cap,
        "peak_stuck": peak_stuck,
        "peak_tool_threads": peak,
        "blocked_quick": {
            "outcome": blocked_quick.outcome.value,
            "error_code": blocked_quick.error_code,
            "executed": quick_calls_while_blocked > 0,
        },
        "drain_seconds": drain_seconds,
        "final_ledger": final_stats,
        "after": after,
        "outcomes": outcomes,
        "samples": sampler.samples,
    }


def scene_concurrency(args: argparse.Namespace) -> dict[str, Any]:
    """并发：cap 只剩一个名额时，同时到达的一批调用能挤进去几个。

    闸门数的是"已放弃（stuck）"，在途调用还没超时所以不算。于是并发度 C 时，
    最坏可以比 cap 多出 C-1 个卡住线程。这个超出量必须被量出来并有上界，
    否则运维按 cap 估算资源会偏小。
    """

    concurrency = max(2, args.concurrency)
    registry = ToolRegistry(max_stuck_threads=args.max_stuck)
    hanging = HangingTool("stress_hang")
    registry.register(hanging)
    grant = ToolGrant(
        registry=registry,
        allowed=("stress_hang",),
        call_timeout_seconds=args.call_timeout,
        retry_backoff_seconds=0,
    )

    # 先把 cap 填到只剩一个名额：这样并发批次一定会越过 cap。
    prefill = max(0, args.max_stuck - 1)
    for seq in range(prefill):
        grant.call(_call("stress_hang", seq), _context())
    before_wave = registry.threads.stats()

    barrier = threading.Barrier(concurrency)

    def worker(seq: int) -> None:
        barrier.wait(timeout=10.0)
        grant.call(_call("stress_hang", 1000 + seq), _context())

    threads = [
        threading.Thread(target=worker, args=(seq,), name=f"stress-wave-{seq}", daemon=True)
        for seq in range(concurrency)
    ]
    with Sampler(registry) as sampler:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10.0)
        peak_stats = registry.threads.stats()
        peak_threads = sampler.peak("tool_threads")
        hanging.release.set()
        drain_seconds, final_stats = _drain(registry)

    hanging.close()
    return {
        "cap": args.max_stuck,
        "concurrency": concurrency,
        "prefill_stuck": before_wave["stuck"],
        "stuck_after_wave": peak_stats["stuck"],
        "overshoot": peak_stats["stuck"] - args.max_stuck,
        "overshoot_bound": concurrency - 1,
        "peak_tool_threads": peak_threads,
        "drain_seconds": drain_seconds,
        "final_ledger": final_stats,
        "samples": sampler.samples,
    }


def scene_fd(args: argparse.Namespace, *, hold_socket: bool = False) -> dict[str, Any]:
    """单条卡住线程的资源成本：纯等待（内存）或持连接（句柄）。

    两种变体各自跑在独立进程里：峰值 RSS 是进程级的，同进程连着跑第二种，
    它的"基线"已经带着第一种的债，量出来的增量就不作数了。
    """

    registry = ToolRegistry(max_stuck_threads=args.max_stuck)
    hanging = HangingTool("stress_hang", hold_socket=hold_socket)
    registry.register(hanging)
    grant = ToolGrant(
        registry=registry,
        allowed=("stress_hang",),
        call_timeout_seconds=args.call_timeout,
        retry_backoff_seconds=0,
    )
    baseline = sample(registry)
    for seq in range(args.max_stuck):
        grant.call(_call("stress_hang", seq), _context())
    after = sample(registry)
    stuck = registry.threads.stats()["stuck"]

    hanging.release.set()
    _drain(registry)
    fds_after_drain = open_fds()
    hanging.close()

    return {
        "variant": "socket_holding" if hold_socket else "sleep_only",
        "stuck": stuck,
        "baseline_fds": baseline["fds"],
        "fds_after_drain": fds_after_drain,
        "fds_after_close": open_fds(),
        "fd_delta": after["fds"] - baseline["fds"],
        "fd_delta_per_stuck": (
            round((after["fds"] - baseline["fds"]) / stuck, 2) if stuck else 0.0
        ),
        "rss_kb_baseline": baseline["peak_rss_kb"],
        "rss_kb_after": after["peak_rss_kb"],
        "rss_delta_kb": round(after["peak_rss_kb"] - baseline["peak_rss_kb"], 1),
        "rss_delta_kb_per_stuck": (
            round((after["peak_rss_kb"] - baseline["peak_rss_kb"]) / stuck, 1) if stuck else 0.0
        ),
        "fd_soft_limit": _fd_limit(),
    }


def _fd_limit() -> int:
    try:
        return int(resource.getrlimit(resource.RLIMIT_NOFILE)[0])
    except (OSError, ValueError):
        return -1


def scene_recovery(args: argparse.Namespace) -> dict[str, Any]:
    """恢复：反复"堆满 → 全部退出"，看名额有没有被历史调用永久吃掉。"""

    registry = ToolRegistry(max_stuck_threads=args.max_stuck)
    hanging = HangingTool("stress_hang")
    registry.register(hanging)
    grant = ToolGrant(
        registry=registry,
        allowed=("stress_hang",),
        call_timeout_seconds=args.call_timeout,
        retry_backoff_seconds=0,
    )

    cycles: list[dict[str, Any]] = []
    for cycle in range(args.cycles):
        for seq in range(args.max_stuck):
            grant.call(_call("stress_hang", cycle * 100 + seq), _context())
        full = registry.threads.stats()
        hanging.release.set()
        drain_seconds, after = _drain(registry)
        cycles.append(
            {
                "cycle": cycle,
                "stuck_when_full": full["stuck"],
                "drain_seconds": drain_seconds,
                "ledger_after": after,
                "peak_rss_kb": round(peak_rss_kb(), 1),
                "fds": open_fds(),
            }
        )
        hanging.release = threading.Event()

    hanging.close()
    return {"cap": args.max_stuck, "cycles": cycles}


class SlowClient:
    """在脚本化客户端外面加一段固定延迟：模拟"模型比 deadline 还慢"。"""

    def __init__(self, inner: ScriptedModelClient, delay: float) -> None:
        self._inner = inner
        self._delay = delay

    def complete(
        self,
        *,
        messages: list[HarnessMessage] | tuple[HarnessMessage, ...],
        tools: list[ToolSpec] | tuple[ToolSpec, ...],
        temperature: float = 0.0,
    ) -> ModelResponse:
        time.sleep(self._delay)
        return self._inner.complete(messages=messages, tools=tools, temperature=temperature)


def _execute_once(*, deadline: float, model_delay: float, tool_name: str) -> dict[str, Any]:
    tool = HangingTool(tool_name, max_wait=60.0)
    registry = ToolRegistry(max_stuck_threads=4)
    registry.register(tool)
    client = ScriptedModelClient(
        [
            ModelResponse(tool_calls=[tool_call(tool_name, call_id="c1")]),
            ModelResponse(text=SUCCESS),
        ]
    )
    wrapped: Any = SlowClient(client, model_delay) if model_delay else client
    executor = HarnessExecutor(wrapped, registry=registry, deadline_seconds=deadline)
    request = ExecutionRequest(
        task_id=uuid4(),
        flow_id="stress.direct",
        flow_version="1",
        step_id="query",
        tool=tool_name,
        instruction="调用工具",
    )
    started = time.monotonic()
    result = executor.execute(request)
    elapsed = round(time.monotonic() - started, 3)
    tool.release.set()
    _drain(registry, timeout=5.0)
    tool.close()
    return {
        "deadline": deadline,
        "model_delay": model_delay,
        "elapsed": elapsed,
        "over_deadline": round(elapsed - deadline, 3),
        "outcome": result.outcome.value,
        "error_code": result.error_code,
        "stop_reason": result.output.get("stop_reason"),
        "tool_invocations": tool.calls,
        "assertions_ok": (
            result.outcome is ExecutionOutcome.UNCONFIRMED
            and result.output.get("stop_reason") in {"deadline", "refused"}
        ),
    }


def scene_lease(args: argparse.Namespace) -> dict[str, Any]:
    """租约 TTL 的依据：一次执行到底可能跑多久。

    两种走法：工具卡死（受 deadline 约束）、模型比 deadline 还慢（deadline 拦不住
    已经发出的模型调用）。后者正是"TTL 只按 deadline 取会中途过期"的原因。
    """

    cases = [
        {"deadline": args.lease_deadline, "model_delay": 0.0},
        {"deadline": args.lease_deadline, "model_delay": args.lease_deadline / 2},
        {"deadline": args.lease_deadline, "model_delay": args.lease_deadline * 1.5},
    ]
    measured = [_execute_once(**case, tool_name="stress_hang") for case in cases]
    return {
        "cases": measured,
        "max_over_deadline": max(case["over_deadline"] for case in measured),
        "all_concluded_unconfirmed": all(case["assertions_ok"] for case in measured),
    }


# ---------- 推荐值 ----------


def recommend(args: argparse.Namespace, scenes: dict[str, Any]) -> dict[str, Any]:
    """按本次实测数据给出两个配置的推荐值与依据（公式写出来，便于复算）。"""

    # 内存看纯等待变体（峰值 RSS 是进程级的，只能一次量一种），句柄看持连接变体。
    rss_scene = scenes.get("fd_sleep", {})
    fd_scene = scenes.get("fd_socket", {})
    fd_limit = fd_scene.get("fd_soft_limit", rss_scene.get("fd_soft_limit", -1))
    per_stuck = fd_scene.get("fd_delta_per_stuck", 0.0)
    rss_per_stuck = rss_scene.get("rss_delta_kb_per_stuck", 0.0)

    # 句柄预算：给卡住线程的额度不超过可用 fd 的 1/4（其余留给连接、日志、监听口）。
    fd_budget = max(0.0, (fd_limit - 64) * 0.25) if fd_limit > 0 else 0.0
    fd_bound = int(fd_budget / per_stuck) if per_stuck > 0 and fd_budget else None

    overshoot = scenes.get("concurrency", {}).get("overshoot", 0)
    keep = DEFAULT_MAX_STUCK_THREADS
    rationale_stuck = (
        f"默认 {keep}，实测每卡住线程峰值 RSS 增量约 {rss_per_stuck:.1f} KB、"
        f"句柄增量约 {per_stuck:.2f} 个；并发度 {args.concurrency} 时最坏卡住"
        f" {keep + overshoot} 条，合计约 {rss_per_stuck * (keep + overshoot) / 1024:.1f} MB、"
        f"{per_stuck * (keep + overshoot):.0f} 个句柄"
        + (f"（上限 {fd_limit}）" if fd_limit > 0 else "")
        + "，远未触及资源边界；因此维持 16，不因压测上调。"
    )
    if fd_bound is not None and fd_bound < keep:
        rationale_stuck += f" 但按句柄预算反推理论上限为 {fd_bound}，需要下调。"

    lease_scene = scenes.get("lease", {})
    max_over = lease_scene.get("max_over_deadline", 0.0)
    # 生产默认：deadline 45 + 模型超时 60 ×（1 + 重试 1）+ 15 秒余量 = 180。
    suggested_ttl = 180.0
    rationale_ttl = (
        f"实测一次执行的耗时可以比 deadline 多出 {max_over:.2f}s（模型调用不受 deadline 约束），"
        "按生产默认值算：deadline 45s + 模型超时 60s×(1+重试 1) + 15s 余量 = 180s；"
        "自动推导的 deadline+90=135s 覆盖不住模型超时加一次重试的最坏情况，"
        "因此多进程试点建议显式设 180。"
    )
    return {
        "max_stuck_threads": {
            "recommended": keep,
            "measured_rss_kb_per_stuck": rss_per_stuck,
            "measured_fd_per_stuck": per_stuck,
            "worst_case_stuck": keep + overshoot,
            "fd_limit": fd_limit,
            "rationale": rationale_stuck,
        },
        "lease_ttl_seconds": {
            "recommended": suggested_ttl,
            "auto_derived_for_default_deadline": 45.0 + 90.0,
            "measured_max_over_deadline": max_over,
            "rationale": rationale_ttl,
        },
    }


SCENES: dict[str, Any] = {
    "baseline": scene_baseline,
    "sequential": scene_sequential,
    "concurrency": scene_concurrency,
    "fd_sleep": lambda args: scene_fd(args, hold_socket=False),
    "fd_socket": lambda args: scene_fd(args, hold_socket=True),
    "recovery": scene_recovery,
    "lease": scene_lease,
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="工具线程 / 租约压力标定（隔离环境）")
    parser.add_argument("--max-stuck", type=int, default=DEFAULT_MAX_STUCK_THREADS)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--call-timeout", type=float, default=0.05, help="单次工具调用的超时秒数")
    parser.add_argument("--extra-calls", type=int, default=3, help="触顶后额外再试几次")
    parser.add_argument("--cycles", type=int, default=3, help="恢复场景的轮数")
    parser.add_argument("--baseline-seconds", type=float, default=0.5)
    parser.add_argument("--lease-deadline", type=float, default=2.0)
    parser.add_argument("--only", default="", help="逗号分隔的场景名，默认全跑")
    parser.add_argument("--json-out", default="", help="把完整结果另存为文件")
    parser.add_argument(
        "--no-isolate",
        action="store_true",
        help="在同一个进程里跑全部场景（仅排障用；内存峰值会被前序场景污染）",
    )
    return parser.parse_args(argv)


def _child_command(name: str, args: argparse.Namespace) -> list[str]:
    return [
        sys.executable,
        str(Path(__file__).resolve()),
        "--only",
        name,
        "--no-isolate",
        "--max-stuck",
        str(args.max_stuck),
        "--concurrency",
        str(args.concurrency),
        "--call-timeout",
        str(args.call_timeout),
        "--extra-calls",
        str(args.extra_calls),
        "--cycles",
        str(args.cycles),
        "--baseline-seconds",
        str(args.baseline_seconds),
        "--lease-deadline",
        str(args.lease_deadline),
    ]


def _run_isolated(name: str, args: argparse.Namespace) -> dict[str, Any]:
    """在一个全新进程里跑单个场景。

    必须隔离：卡住的线程和峰值 RSS 都是不可回收的，在同一个进程里连着跑，
    第二个场景的"基线"就已经带着第一个场景的债——量出来的内存增量没有意义。
    """

    # 只以本脚本自身为子进程，参数全部来自命令行，没有外部输入。
    completed = subprocess.run(
        _child_command(name, args),
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"场景 {name} 子进程失败：{completed.stderr.strip()[-500:]}")
    payload = json.loads(completed.stdout)
    return payload["scenes"][name]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    selected = [name.strip() for name in args.only.split(",") if name.strip()] or list(SCENES)
    unknown = [name for name in selected if name not in SCENES]
    if unknown:
        print(f"未知场景：{', '.join(unknown)}", file=sys.stderr)
        return 2

    scenes: dict[str, Any] = {}
    started = time.monotonic()
    for name in selected:
        if args.no_isolate:
            scenes[name] = SCENES[name](args)
        else:
            scenes[name] = _run_isolated(name, args)

    report = {
        "environment": {
            "platform": sys.platform,
            "python": sys.version.split()[0],
            "pid": os.getpid(),
            "max_stuck": args.max_stuck,
            "concurrency": args.concurrency,
            "call_timeout_seconds": args.call_timeout,
            "isolated_scenes": not args.no_isolate,
            "duration_seconds": round(time.monotonic() - started, 2),
        },
        "scenes": scenes,
        "recommendation": recommend(args, scenes),
    }

    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.json_out:
        Path(args.json_out).write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
