"""压测脚本自身的回归：它报出来的结论必须仍然成立。

`scripts/stress_tool_threads.py` 是第 21 节那两个上限的依据。脚本一旦被改动
（比如误把容量闸门的判定顺序换掉），报告会继续输出"看起来正常"的数字，
而结论已经错了——所以这里用极小参数真跑一遍它，钉住三条硬结论：
触顶发生在执行之前、被挡的工具一次都没执行、放行后台账归零。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "stress_tool_threads.py"


def test_stress_script_confirms_capacity_gate_and_recovery() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--only",
            "sequential",
            "--no-isolate",
            "--max-stuck",
            "2",
            "--extra-calls",
            "2",
            "--call-timeout",
            "0.02",
            "--json-out",
            "",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    scene = json.loads(completed.stdout)["scenes"]["sequential"]

    # cap=2：前两次超时计入卡住，第 3 次被闸门挡下。
    assert scene["calls_until_blocked"] == 3
    assert scene["stuck_at_cap"] == {"live": 2, "stuck": 2, "max_stuck": 2}
    # 拒绝发生在执行之前：被挡的工具一次都没被执行。
    assert scene["blocked_quick"] == {
        "outcome": "blocked",
        "error_code": "tool_capacity",
        "executed": False,
    }
    # 放行后名额归还，否则上限会被历史调用永久占死。
    assert scene["final_ledger"] == {"live": 0, "stuck": 0, "max_stuck": 2}
    assert scene["drain_seconds"] < 5.0
