"""多 worker 认领协议的并发自检。

关键不变量：任意时刻，一个 Issue 的有效 claim 里 id 最小者唯一，
所以最多一个 worker 能拿到「获准」。测试用带锁的 fake GitHub 模拟真实
并发（服务端分配自增 comment id），而不是把仲裁逻辑按顺序跑两遍。
"""
from __future__ import annotations

import importlib.util
import sys
import threading
from datetime import timedelta
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "issue_claim.py"
_spec = importlib.util.spec_from_file_location("workpanda_issue_claim", MODULE_PATH)
claim = importlib.util.module_from_spec(_spec)
# dataclasses 会回查 sys.modules[cls.__module__]，动态加载必须先登记模块。
sys.modules[_spec.name] = claim
_spec.loader.exec_module(claim)


def test_claim_lock_acquire_release_no_crash():
    # 跨平台锁在「当前平台」取锁 + 释放不得抛异常；Windows 上走 msvcrt、
    # Unix 上走 fcntl，两者皆缺则退化为不锁 + 警告。至少保证不崩溃。
    with claim.claim_lock():
        pass


class FakeGitHub:
    """服务端语义的假实现：comment id 在锁内自增，模拟 GitHub 的单调分配。"""

    def __init__(self, issues: list[dict] | None = None) -> None:
        self._lock = threading.Lock()
        self._next_comment_id = 100
        self.comments: dict[int, list[dict]] = {}
        self.issues = issues or []
        self.pulls: list[dict] = []
        self.branches: list[str] = []
        self.commits: list[dict] = []
        self.check_runs_result: list[dict] = []
        self.issue_state = "open"

    def _issue(self, number: int) -> dict:
        return {
            "number": number,
            "state": self.issue_state,
            "labels": [{"name": "enhancement"}],
            "assignees": [],
        }

    def get_issue(self, number: int) -> dict:
        return self._issue(number)

    def add_comment(self, number: int, body: str) -> dict:
        with self._lock:
            self._next_comment_id += 1
            comment = {"id": self._next_comment_id, "body": body}
            self.comments.setdefault(number, []).append(comment)
            return dict(comment)

    def list_comments(self, number: int) -> list[dict]:
        with self._lock:
            return [dict(item) for item in self.comments.get(number, [])]

    def list_open_issues(self) -> list[dict]:
        return [dict(item) for item in self.issues]

    def list_branches(self) -> list[dict]:
        return [{"name": name} for name in self.branches]

    def list_open_pulls(self) -> list[dict]:
        return [dict(item) for item in self.pulls]

    def list_recent_commits(self) -> list[dict]:
        return [dict(item) for item in self.commits]

    def check_runs(self, sha: str) -> list[dict]:
        return [dict(item) for item in self.check_runs_result]

    def seed_claim(self, worker: str, number: int, now, ttl: int = 7200) -> dict:
        return self.add_comment(number, claim.claim_body(worker, number, now, ttl))


def green_checks() -> list[dict]:
    return [
        {"name": "Python 检查", "status": "completed", "conclusion": "success"},
        {"name": "文档与敏感信息基础检查", "status": "completed", "conclusion": "success"},
    ]


def test_claim_body_roundtrip():
    now = claim.utcnow()
    body = claim.claim_body("worker1", 27, now, 7200)
    claims, released = claim.parse_events([{"id": 101, "body": body}])
    assert released == set()
    assert len(claims) == 1
    assert claims[0].worker_id == "worker1"
    assert claims[0].comment_id == 101
    assert claims[0].ttl_seconds == 7200


def test_parse_events_ignores_malformed_and_foreign_versions():
    now = claim.utcnow()
    comments = [
        {"id": 101, "body": claim.CLAIM_PREFIX + "{broken"},
        {"id": 102, "body": claim.CLAIM_PREFIX + '{"v":99,"worker":"x"}'},
        {"id": 103, "body": "普通讨论，不是认领"},
        {"id": 104, "body": claim.claim_body("worker1", 27, now, 7200)},
    ]
    claims, released = claim.parse_events(comments)
    assert [item.comment_id for item in claims] == [104]
    assert released == set()


def test_arbitrate_smallest_comment_id_wins():
    now = claim.utcnow()
    claims = [
        claim.Claim(102, "worker2", now, 7200),
        claim.Claim(101, "worker1", now, 7200),
    ]
    assert claim.arbitrate(claims, "worker1", now)[0] is True
    assert claim.arbitrate(claims, "worker2", now)[0] is False
    assert claim.arbitrate(claims, "worker2", now)[2] == "lost_arbitration"


def test_arbitrate_expired_claim_is_ignored():
    now = claim.utcnow()
    stale = claim.Claim(101, "worker1", now - timedelta(seconds=7201), 7200)
    fresh = claim.Claim(102, "worker2", now, 7200)
    granted, winner, _ = claim.arbitrate([stale, fresh], "worker2", now)
    assert granted is True
    assert winner.comment_id == 102
    assert claim.arbitrate([stale], "worker1", now)[2] == "no_active_claim"


def test_open_pr_extends_ttl_but_keeps_an_upper_bound():
    now = claim.utcnow()
    stale = claim.Claim(101, "worker1", now - timedelta(seconds=7201), 7200)
    assert claim.arbitrate([stale], "worker1", now, has_open_pr=False)[0] is False
    assert claim.arbitrate([stale], "worker1", now, has_open_pr=True)[0] is True
    ancient = claim.Claim(101, "worker1", now - timedelta(seconds=7200 * 5), 7200)
    assert claim.arbitrate([ancient], "worker1", now, has_open_pr=True)[0] is False


def test_released_claim_no_longer_wins():
    now = claim.utcnow()
    claims = [claim.Claim(101, "worker1", now, 7200), claim.Claim(102, "worker2", now, 7200)]
    granted, winner, _ = claim.arbitrate(claims, "worker2", now, released_ids={101})
    assert granted is True
    assert winner.worker_id == "worker2"


def test_release_is_scoped_to_one_claim_not_the_worker():
    """释放上一轮不能把同一 worker 下一轮的新 claim 也作废。"""

    now = claim.utcnow()
    old = claim.claim_body("worker1", 27, now, 7200)
    new = claim.claim_body("worker1", 27, now, 7200)
    release = claim.release_body("worker1", 27, now, 101)
    claims, released_ids = claim.parse_events(
        [{"id": 101, "body": old}, {"id": 102, "body": release}, {"id": 103, "body": new}]
    )
    assert released_ids == {101}
    granted, winner, _ = claim.arbitrate(
        claims, "worker1", now, released_ids=released_ids
    )
    assert granted is True
    assert winner.comment_id == 103


def test_old_format_release_without_claim_id_is_ignored():
    """解析不出来的释放不能靠猜补一个 claim id。"""

    now = claim.utcnow()
    stale = claim.RELEASE_PREFIX + '{"v":1,"worker":"worker1"}'
    comments = [
        {"id": 101, "body": claim.claim_body("worker1", 27, now, 7200)},
        {"id": 102, "body": stale},
    ]
    claims, released_ids = claim.parse_events(comments)
    assert released_ids == set()
    assert claim.arbitrate(claims, "worker1", now, released_ids=released_ids)[0] is True


def test_two_workers_concurrent_only_one_granted():
    """核心用例：两个 worker 真正并发认领同一 Issue，有且仅有一个获准。"""

    for _ in range(20):
        fake = FakeGitHub()
        now = claim.utcnow()
        barrier = threading.Barrier(2)
        results: dict[str, dict] = {}
        errors: list[Exception] = []

        def run(worker_id: str) -> None:
            try:
                barrier.wait()
                results[worker_id] = claim.claim_once(fake, 27, worker_id, now, 7200)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            threading.Thread(target=run, args=("worker1",)),
            threading.Thread(target=run, args=("worker2",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert errors == []
        assert len(results) == 2
        winners = [name for name, result in results.items() if result["granted"]]
        assert len(winners) == 1, f"并发下出现了 {len(winners)} 个获准者：{results}"
        loser = "worker2" if winners[0] == "worker1" else "worker1"
        assert results[loser]["reason"] == "lost_arbitration"
        assert results[winners[0]]["claim_comment_id"] == min(
            result["claim_comment_id"] for result in results.values()
        )
        assert results[winners[0]]["branch"] == f"issue/27-{winners[0]}"


def test_claim_once_refuses_closed_issue():
    fake = FakeGitHub()
    fake.issue_state = "closed"
    result = claim.claim_once(fake, 27, "worker1", claim.utcnow(), 7200)
    assert result["granted"] is False
    assert result["reason"] == "issue_not_open"
    assert fake.list_comments(27) == []


def test_eligible_only_unclaimed_enhancement():
    now = claim.utcnow()
    issues = [
        {"number": 27, "state": "open", "labels": [{"name": "enhancement"}], "assignees": []},
        {"number": 28, "state": "open", "labels": [{"name": "enhancement"}], "assignees": []},
        {"number": 30, "state": "open", "labels": [{"name": "question"}], "assignees": []},
        {"number": 37, "state": "open", "labels": [{"name": "documentation"}], "assignees": []},
    ]
    fake = FakeGitHub(issues)
    fake.seed_claim("worker1", 27, now)
    assert claim.eligible_issues(fake, now, 7200) == [28]


def test_eligible_skips_issue_with_existing_branch():
    now = claim.utcnow()
    issues = [
        {"number": 27, "state": "open", "labels": [{"name": "enhancement"}], "assignees": []},
    ]
    fake = FakeGitHub(issues)
    fake.branches = ["issue/27-worker1"]
    assert claim.eligible_issues(fake, now, 7200) == []


def test_guard_passes_only_when_claim_still_mine_and_ci_green():
    now = claim.utcnow()
    fake = FakeGitHub()
    fake.seed_claim("worker1", 27, now)
    fake.check_runs_result = green_checks()
    result = claim.guard(fake, 27, "worker1", "abc123", now)
    assert result["ok"] is True
    assert result["checks"] == 2


def test_guard_rejects_after_release():
    now = claim.utcnow()
    fake = FakeGitHub()
    seeded = fake.seed_claim("worker1", 27, now)
    fake.add_comment(27, claim.release_body("worker1", 27, now, seeded["id"]))
    fake.check_runs_result = green_checks()
    result = claim.guard(fake, 27, "worker1", "abc123", now)
    assert result["ok"] is False
    assert result["reason"].startswith("claim_lost_or_expired")


def test_loser_withdraws_so_issue_becomes_reclaimable():
    """真实 API 验证暴露的回归：判输者不能留下一条占位的 claim。"""

    now = claim.utcnow()
    issues = [
        {"number": 27, "state": "open", "labels": [{"name": "enhancement"}], "assignees": []},
    ]
    fake = FakeGitHub(issues)
    first = claim.claim_once(fake, 27, "worker1", now, 7200)
    second = claim.claim_once(fake, 27, "worker2", now, 7200)
    assert first["granted"] is True
    assert second["granted"] is False
    assert claim.eligible_issues(fake, now, 7200) == []

    released = claim.release_claim(fake, 27, "worker1", now)
    assert released["ok"] is True
    assert released["released_claim_id"] == first["claim_comment_id"]
    assert claim.eligible_issues(fake, now, 7200) == [27]


def test_release_refuses_when_no_claim_owned():
    fake = FakeGitHub()
    result = claim.release_claim(fake, 27, "worker1", claim.utcnow())
    assert result["ok"] is False
    assert result["reason"].startswith("no_active_claim_to_release")
    assert fake.list_comments(27) == []


def test_guard_rejects_red_or_missing_ci():
    now = claim.utcnow()
    fake = FakeGitHub()
    fake.seed_claim("worker1", 27, now)
    fake.check_runs_result = [
        {"name": "Python 检查", "status": "completed", "conclusion": "failure"}
    ]
    assert claim.guard(fake, 27, "worker1", "abc123", now)["reason"] == "ci_not_green"
    fake.check_runs_result = []
    assert claim.guard(fake, 27, "worker1", "abc123", now)["reason"] == "ci_not_reported"


def test_guard_rejects_competing_branch_and_already_merged():
    now = claim.utcnow()
    fake = FakeGitHub()
    fake.seed_claim("worker1", 27, now)
    fake.check_runs_result = green_checks()
    fake.pulls = [{"head": {"ref": "issue/27-worker2"}}]
    assert claim.guard(fake, 27, "worker1", "abc123", now)["reason"].startswith("competing_branch")
    fake.pulls = []
    fake.commits = [{"commit": {"message": "feat: 修完 (#28)\n\nCloses #27"}}]
    assert claim.guard(fake, 27, "worker1", "abc123", now)["reason"] == "already_merged"


def test_default_lock_path_is_outside_repo(monkeypatch):
    monkeypatch.delenv("WORKPANDA_CLAIM_LOCK", raising=False)
    path = claim.lock_path()
    assert ".workbuddy/locks" in str(path)
    assert "Desktop/projects/WorkPanda" not in str(path)
