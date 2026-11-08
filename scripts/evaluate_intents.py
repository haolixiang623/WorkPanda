"""识别评测：CI 与运维入口。

用法：

    # 评全部已发布流程的样例（CI 就跑这一条）
    python3 scripts/evaluate_intents.py

    # 机器可读（CI 归档 / 比对两次结果是否一致）
    python3 scripts/evaluate_intents.py --json

    # 真实事项就位后不再装载骨架自检流程
    python3 scripts/evaluate_intents.py --no-demo

退出码：0 = 通过（无误路由、漏路由不超限、样例条数达标）；1 = 未通过或**没有结论**
（评测超时/异常）；2 = 用法错误。

刻意不构造 `Settings`：这个入口只要流程定义与规则，不该顺带读取 `.env` 里的模型凭据。
默认值直接取自 `domain/` 的常量，与 `Settings` 的默认值同源——门禁与 CI 用同一套数字，
否则"本地过了 CI 不过"只会被当成环境问题。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from workpanda.application.intent_eval import IntentEvalRunner  # noqa: E402
from workpanda.bootstrap import build_intent_router, seed_demo_flows  # noqa: E402
from workpanda.domain.flow import FlowRegistry, FlowStatus  # noqa: E402
from workpanda.domain.intent_eval import (  # noqa: E402
    DEFAULT_EVAL_TIMEOUT_SECONDS,
    DEFAULT_MAX_MISS_RATE,
    IntentEvalReport,
)
from workpanda.domain.intent_sample import (  # noqa: E402
    MIN_NEAR_MISS_SAMPLES,
    MIN_POSITIVE_SAMPLES,
)
from workpanda.infrastructure.memory_store import InMemoryFlowStore  # noqa: E402


def load_registry(*, with_demo: bool) -> FlowRegistry:
    """流程目录。与启动路径同一套派生：发布状态由指针决定，不看版本里的作者态标记。"""

    store = InMemoryFlowStore()
    seed_demo_flows(store, enabled=with_demo)
    return FlowRegistry.from_store(store)


def run_eval(
    registry: FlowRegistry,
    *,
    timeout_seconds: float,
    max_miss_rate: float,
    min_positive: int,
    min_near_miss: int,
) -> IntentEvalReport:
    """评"发布口径"的语料。规则装配走 `bootstrap.build_intent_router`，与服务端同源。

    规则取自**已发布版本**（`FlowDefinition.intent_rules`，D2=B）：草稿版本的
    规则如果也进路由器，它会去抢已发布版本的样例，评出来的是"两版互相打架"，
    不是线上真实口径。
    """

    published = [flow for flow in registry.versions() if flow.status is FlowStatus.PUBLISHED]
    router = build_intent_router(published)
    return IntentEvalRunner(timeout_seconds=timeout_seconds).run(
        published,
        router,
        min_positive=min_positive,
        min_near_miss=min_near_miss,
        max_miss_rate=max_miss_rate,
    )


def _print_report(report: IntentEvalReport) -> None:
    sample_total = len(report.results)
    print(f"识别评测：{len(report.flows)} 个已发布流程，{sample_total} 条样例")
    print(f"流程：{'、'.join(report.flows) or '（无）'}")

    for row in report.coverage:
        marker = "缺" if row.short else "够"
        print(f"  覆盖 {row.flow_id}@{row.flow_version}：{row.describe()}（{marker}）")

    print("混淆矩阵（行 = 期望，列 = 实际）：")
    if not report.matrix:
        print("  （没有样例，矩阵为空）")
    for cell in report.matrix:
        print(f"  期望 {cell.expected} → 实际 {cell.actual}：{cell.count}")

    print(
        f"误路由 {report.misroute_count} 条（要求 0）；"
        f"漏路由 {len(report.missed)}/{report.positive_total}"
        f"（{report.miss_rate:.1%}，上限 {report.max_miss_rate:.1%}）"
    )
    for finding in report.findings:
        level = "阻断" if finding.blocking else "记录"
        print(f"  [{level}] {finding.code.value}：{finding.detail}")
    print(f"结论：{_verdict_label(report)}")


def _verdict_label(report: IntentEvalReport) -> str:
    if report.status.value == "unconfirmed":
        return "无结论（评测超时或异常，不算通过）"
    return "通过" if report.publishable else "未通过"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="识别评测（发布门禁的 CI 入口）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出报告")
    parser.add_argument(
        "--no-demo",
        action="store_true",
        help="不装载骨架自检流程（真实事项就位后使用）",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_EVAL_TIMEOUT_SECONDS,
        help="评测时限（秒）；超时判无结论",
    )
    parser.add_argument(
        "--max-miss-rate",
        type=float,
        default=DEFAULT_MAX_MISS_RATE,
        help="漏路由率上限；误路由没有开关，必须为 0",
    )
    parser.add_argument(
        "--min-positive",
        type=int,
        default=MIN_POSITIVE_SAMPLES,
        help="每个已发布流程所需的最少正例数",
    )
    parser.add_argument(
        "--min-near-miss",
        type=int,
        default=MIN_NEAR_MISS_SAMPLES,
        help="每个已发布流程所需的最少近似误例数",
    )
    args = parser.parse_args(argv)

    if args.timeout <= 0:
        print("--timeout 必须大于 0", file=sys.stderr)
        return 2

    report = run_eval(
        load_registry(with_demo=not args.no_demo),
        timeout_seconds=args.timeout,
        max_miss_rate=args.max_miss_rate,
        min_positive=args.min_positive,
        min_near_miss=args.min_near_miss,
    )

    if args.json:
        print(json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2))
    else:
        _print_report(report)

    if report.publishable:
        return 0
    print(f"识别评测未通过：{_verdict_label(report)}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
