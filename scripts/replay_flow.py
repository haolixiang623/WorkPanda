"""SOP 发布前回放：运维用入口。

用法：

    # 只看这个版本能不能发（不改任何状态）
    python3 scripts/replay_flow.py --flow demo.generic --version 2

    # 走门禁并真的移动发布指针
    python3 scripts/replay_flow.py --flow demo.generic --version 2 --publish

    # 给某个版本加一条用例（用例是数据，写在库里）
    python3 scripts/replay_flow.py --flow demo.generic --version 2 --add-case case.json

    # 删掉一条写错的用例
    python3 scripts/replay_flow.py --flow demo.generic --version 2 --delete-case happy

`--add-case` 的 JSON 只写用例本身（`flow_id` / `flow_version` 取自命令行）：

    {"case_id": "happy", "slots": {"subject": "自检"},
     "expect": {"steps": ["collect_input", "execute_demo"],
                "tools": ["model_echo"], "terminal": "outcome_unconfirmed"},
     "note": "最小正常路径"}

刻意不构造 `Settings`：这个入口只需要一个库文件路径，不该顺带读取 `.env` 里的模型
凭据。默认库文件与 `WORKPANDA_SQLITE_PATH` 一致，可用 `--sqlite` 覆盖。

退出码：0 = 全部用例通过（或发布成功）；1 = 有阻挡项 / 发布被拦；2 = 用法或数据错误。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from workpanda.application.release import FlowReleaseBlocked, FlowReleaseService  # noqa: E402
from workpanda.application.replay import FlowReplay  # noqa: E402
from workpanda.domain.replay import ReplayCase, ReplayCaseExists, ReplayReport  # noqa: E402
from workpanda.infrastructure.replay_cases import SqliteReplayCaseStore  # noqa: E402
from workpanda.infrastructure.sqlite_flow_store import SqliteFlowStore  # noqa: E402


def _default_db() -> Path:
    return Path(os.environ.get("WORKPANDA_SQLITE_PATH") or "workpanda.db")


def _load_case_file(path: Path, flow_id: str, version: str) -> ReplayCase:
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.setdefault("case_id", path.stem)
    payload["flow_id"] = flow_id
    payload["flow_version"] = version
    return ReplayCase.model_validate(payload)


def _print_report(report: ReplayReport) -> None:
    print(f"回放 {report.flow_id}@{report.flow_version}：整体判定 {report.verdict.value}")
    if not report.results:
        print("  （该版本没有回放用例，没有可用的发布依据）")
        return
    for result in report.results:
        marker = "PASS" if result.verdict.value == "pass" else result.verdict.value.upper()
        print(f"  [{marker}] 用例 {result.case_id}（终态 {result.terminal or '未知'}）")
        if result.steps:
            trail = " → ".join(f"{step.step_id}({step.outcome.value})" for step in result.steps)
            print(f"    步骤：{trail}")
        for call in result.tools:
            kind = "有副作用" if call.side_effecting else "只读"
            digest = call.argument_digest[:12]
            print(f"    工具：{call.name} @ {call.step_id}（{kind}，摘要 {digest}）")
        for finding in result.findings:
            print(f"    问题：{finding.code.value} — {finding.detail}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SOP 发布前回放")
    parser.add_argument("--flow", required=True, help="流程 ID")
    parser.add_argument("--version", required=True, help="流程版本")
    parser.add_argument("--sqlite", type=Path, default=None, help="库文件路径")
    parser.add_argument("--timeout", type=float, default=10.0, help="单条用例回放时限（秒）")
    parser.add_argument(
        "--side-effect-free",
        action="append",
        default=[],
        metavar="TOOL",
        help="声明可安全模拟的工具（可重复）；未声明的一律按有副作用记账",
    )
    parser.add_argument("--publish", action="store_true", help="通过门禁后移动发布指针")
    parser.add_argument("--add-case", type=Path, default=None, help="从 JSON 文件新增用例")
    parser.add_argument("--delete-case", default=None, metavar="CASE_ID", help="删除一条用例")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出报告")
    args = parser.parse_args(argv)

    path = args.sqlite or _default_db()
    if not path.exists():
        print(f"库文件不存在：{path}（先运行一次服务，或指定 --sqlite）", file=sys.stderr)
        return 2

    flows = SqliteFlowStore(path)
    cases = SqliteReplayCaseStore(path)
    try:
        if args.delete_case:
            deleted = cases.delete(args.flow, args.version, args.delete_case)
            print(f"{'已删除' if deleted else '未找到'}用例 {args.delete_case}")
            return 0 if deleted else 2

        if args.add_case:
            case = _load_case_file(args.add_case, args.flow, args.version)
            try:
                cases.add(case)
            except ReplayCaseExists as exc:
                print(f"用例已存在：{exc}", file=sys.stderr)
                return 2
            print(f"已新增用例 {case.case_id}（{args.flow}@{args.version}）")
            return 0

        side_effect_free = frozenset(args.side_effect_free)
        replay = FlowReplay(
            timeout_seconds=args.timeout,
            side_effecting=lambda name: name not in side_effect_free,
        )
        service = FlowReleaseService(
            flows=flows,
            cases=cases,
            replay=replay,
        )

        if args.publish:
            try:
                outcome = service.release(args.flow, args.version)
            except FlowReleaseBlocked as exc:
                _emit(exc.report, args.json)
                print(f"发布被拦：{exc}", file=sys.stderr)
                return 1
            if outcome.report is not None:
                _emit(outcome.report, args.json)
            else:
                print(f"该版本没有回放用例，门禁不适用；已发布 {args.flow}@{args.version}")
            return 0

        report = service.check(args.flow, args.version)
        _emit(report, args.json)
        return 0 if report.publishable else 1
    finally:
        cases.close()
        flows.close()


def _emit(report: ReplayReport, as_json: bool) -> None:
    if as_json:
        print(json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2))
        return
    _print_report(report)


if __name__ == "__main__":
    raise SystemExit(main())
