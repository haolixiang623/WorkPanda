"""按用例回放一条流程版本，用于发布前自检。

回放全程不触达真实系统：工具返回来自用例脚本，任务落在内存库里，发布指针不会被移动。

用法：

    python scripts/replay_flow.py cases/demo.generic.json
    python scripts/replay_flow.py cases/*.json --sqlite-path workpanda.db

退出码：

    0  全部通过
    1  有未通过的用例，或有"用例不完整"（缺脚本、缺字段）
    2  前置条件不满足（用例读不出来、库里找不到该版本）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 与其它脚本一致：直接把仓库的 src 放到最前，避免依赖某个已安装的 workpanda。
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from workpanda.application.replay import FlowReplayer, ReplayCase, ReplayReport  # noqa: E402
from workpanda.bootstrap import build_replayer, replay_ports  # noqa: E402
from workpanda.config import Settings  # noqa: E402
from workpanda.domain.flow import FlowDefinition  # noqa: E402
from workpanda.infrastructure.sqlite_flow_store import SqliteFlowStore  # noqa: E402


def load_case(path: Path) -> ReplayCase:
    return ReplayCase.model_validate(json.loads(path.read_text(encoding="utf-8")))


def find_version(versions: list[FlowDefinition], case: ReplayCase) -> FlowDefinition:
    for flow in versions:
        if (flow.flow_id, flow.version) == (case.flow_id, case.flow_version):
            return flow
    raise LookupError(
        f"库里没有流程 {case.flow_id}@{case.flow_version}（用例 {case.case_id}）；"
        "先写入该版本再回放"
    )


def run_case(
    replayer: FlowReplayer,
    flow: FlowDefinition,
    case: ReplayCase,
) -> ReplayReport:
    store, confirmations, publisher = replay_ports()
    return replayer.replay(
        flow,
        case,
        store=store,
        confirmations=confirmations,
        publisher=publisher,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按用例回放流程版本")
    parser.add_argument("cases", nargs="+", type=Path, help="用例 JSON 文件")
    parser.add_argument("--sqlite-path", type=Path, default=None, help="流程库路径")
    args = parser.parse_args(argv)

    settings = Settings()
    if args.sqlite_path is not None:
        settings = settings.model_copy(update={"sqlite_path": args.sqlite_path})

    # 直接打开 SQLite 流程库，不走 `build_flow_store`：后者跟随 `store_backend`，
    # 而回放的对象永远是"库里那条版本"，不该因为内存/文件后端的选择而读不到。
    flow_store = SqliteFlowStore(settings.sqlite_path)
    replayer = build_replayer(settings)
    failed = 0
    try:
        for path in args.cases:
            try:
                case = load_case(path)
                flow = find_version(flow_store.list_versions(case.flow_id), case)
            except (OSError, ValueError, LookupError) as exc:
                print(f"{path}: {exc}", file=sys.stderr)
                return 2
            report = run_case(replayer, flow, case)
            print(json.dumps(report.model_dump(mode="json"), ensure_ascii=False))
            if not report.passed:
                failed += 1
                reason = report.incomplete_reason or "；".join(
                    check.detail for check in report.checks if not check.passed
                )
                print(f"{path}: 未通过 —— {reason}", file=sys.stderr)
    finally:
        flow_store.close()

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
