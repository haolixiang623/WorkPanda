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


def test_open_pr_extends_ttl_only_for_the_worker_that_opened_it():
    """放宽有效期按 claim 归属算，不按 Issue 全局算。

    这条用例的前身（test_open_pr_extends_ttl_but_keeps_an_upper_bound）断言
    「该 Issue 上有 open PR → 任何过期的 claim 都复活」，正是这条写错的语义
    让一条从未产出、早已过期的旧 claim 凭更小的 comment id 判赢，把真正在
    干活、PR 都已经开出来的 worker 挤掉（#71）。这里纠正的是一条错误断言，
    不是为了让测试变绿而改期望。
    """

    now = claim.utcnow()
    stale = claim.Claim(101, "worker1", now - timedelta(seconds=7201), 7200)

    # 没产出过 PR：到点即失效，别人开 PR 也延长不了我的租约。
    assert claim.arbitrate([stale], "worker1", now)[0] is False
    assert claim.arbitrate([stale], "worker1", now, open_pr_workers={"worker2"})[0] is False

    # 自己的 claim + 自己的 PR：放宽。
    assert claim.arbitrate([stale], "worker1", now, open_pr_workers={"worker1"})[0] is True

    # 放宽仍有上界：一条悬空 PR 不能把 Issue 永久锁死。
    ancient = claim.Claim(101, "worker1", now - timedelta(seconds=7200 * 5), 7200)
    assert claim.arbitrate([ancient], "worker1", now, open_pr_workers={"worker1"})[0] is False


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


def test_open_pr_workers_parses_worker_from_branch_name():
    """PR 归属从分支名 issue/<n>-<worker> 里取，越界的一律不算进来。"""

    pulls = [
        {"head": {"ref": "issue/71-worker1"}},
        {"head": {"ref": "issue/71-interactive"}},
        {"head": {"ref": "issue/711-worker9"}},  # 不同 Issue
        {"head": {"ref": "issue/71-"}},  # 空 worker
        {"head": {"ref": "feature/issue/71-x"}},
        {"head": {}},
    ]
    assert claim.open_pr_workers(pulls, 71) == {"worker1", "interactive"}


def test_stale_claim_without_output_cannot_be_revived_by_another_pr():
    """#71 真实事故的回归。

    #68 / #69 两份已完成、CI 全绿的实现双双 lost_arbitration，因为一条
    06:18:06Z 创建、从未产出、08:18:06Z 已过期的 claim（comment
    6718147034）被「该 Issue 上有 open PR」的全局倍率复活，凭更小的
    comment id 判赢。
    """

    now = claim.utcnow()
    fake = FakeGitHub()
    fake.seed_claim("worker3", 71, now - timedelta(seconds=7201))  # id 更小，但没产出
    fake.pulls = [{"head": {"ref": "issue/71-worker1"}}]  # PR 是 worker1 的

    result = claim.claim_once(fake, 71, "worker1", now, 7200)
    assert result["granted"] is True
    assert result["reason"] == "granted"
    assert result["winner_comment_id"] == result["claim_comment_id"]
    assert result["branch"] == "issue/71-worker1"


def test_active_claims_ignores_stale_claim_even_when_others_have_open_pr():
    """list / next 走的是 active_claims，必须与 arbitrate 同一套过期规则。"""

    now = claim.utcnow()
    stale = claim.Claim(101, "worker3", now - timedelta(seconds=7201), 7200)
    fresh = claim.Claim(102, "worker1", now, 7200)

    alive = claim.active_claims(
        [stale, fresh], now, released_ids=set(), open_pr_workers={"worker1"}
    )
    assert [item.comment_id for item in alive] == [102]

    # 只有当 worker3 自己也有了 PR，它这条才配留下
    alive = claim.active_claims(
        [stale, fresh], now, released_ids=set(), open_pr_workers={"worker1", "worker3"}
    )
    assert [item.comment_id for item in alive] == [101, 102]


def test_guard_does_not_revive_another_workers_stale_claim():
    """合并前闸门同样不许让别人的过期 claim 复活。"""

    now = claim.utcnow()
    fake = FakeGitHub()
    fake.seed_claim("worker3", 71, now - timedelta(seconds=7201))
    fake.seed_claim("worker1", 71, now)
    fake.pulls = [{"head": {"ref": "issue/71-worker1"}}]
    fake.check_runs_result = green_checks()

    result = claim.guard(fake, 71, "worker1", "abc123", now)
    assert result["ok"] is True, result


def test_claim_guard_and_list_agree_on_the_same_server_state():
    """#71 一致性验收：claim / guard / list 对同一份服务端状态结论相同。"""

    now = claim.utcnow()
    issues = [
        {"number": 71, "state": "open", "labels": [{"name": "enhancement"}], "assignees": []},
    ]
    fake = FakeGitHub(issues)
    fake.seed_claim("worker3", 71, now - timedelta(seconds=7201))
    fake.pulls = [{"head": {"ref": "issue/71-worker1"}}]
    fake.check_runs_result = green_checks()

    assert claim.claim_once(fake, 71, "worker1", now, 7200)["granted"] is True
    assert claim.guard(fake, 71, "worker1", "abc123", now)["ok"] is True
    assert claim.eligible_issues(fake, now, 7200) == []
    # 过期无产出的 worker3 手上没有任何可释放的 claim
    released = claim.release_claim(fake, 71, "worker3", now)
    assert released["ok"] is False
    assert released["reason"].startswith("no_active_claim_to_release")


def test_default_lock_path_is_outside_repo(monkeypatch):
    monkeypatch.delenv("WORKPANDA_CLAIM_LOCK", raising=False)
    path = claim.lock_path()
    assert ".workbuddy/locks" in str(path)
    assert "Desktop/projects/WorkPanda" not in str(path)
