"""多 worker 的 Issue 认领协议。

权威判定不在 assignee / label（GitHub 的 assignee 写入是追加语义，不是排他锁），
而在 claim comment 的服务端单调 id：一个 Issue 的有效 claim 中 id 最小者获准。
comment id 由 GitHub 分配且唯一，所以最小者唯一 —— 这与并发时序无关。

本机的 flock 只保证「挑选 Issue → 发 claim」这一段不撞车；跨主机时 flock 失效，
但 comment id 仲裁仍然成立，结果只是一次无效认领，不会有两个 worker 同时开工。

子命令：
    list                 列出可自动认领的 Issue（只读诊断）
    next                 领下一个可认领的 Issue（worker 主入口）
    claim <issue>        领指定 Issue
    release <issue>      释放自己的认领
    guard <issue>        合并前复核（L3 闸门）

凭据：优先 GITHUB_TOKEN，否则用 `git credential fill` 取，不入库。
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

CLAIM_PREFIX = "[workpanda-claim] "
RELEASE_PREFIX = "[workpanda-release] "
DEFAULT_REPO = "haolixiang623/WorkPanda"
DEFAULT_TTL_SECONDS = 7200
PR_TTL_MULTIPLIER = 4
ELIGIBLE_LABEL = "enhancement"
PROXY = "http://127.0.0.1:7897"
API_ROOT = "https://api.github.com"


# ---------------------------------------------------------------------------
# 纯函数：事件解析与仲裁（不碰网络，便于并发自检）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Claim:
    comment_id: int
    worker_id: str
    created_at: datetime
    ttl_seconds: int


def utcnow() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def claim_body(worker_id: str, issue_number: int, now: datetime, ttl: int) -> str:
    payload = {
        "v": 1,
        "worker": worker_id,
        "issue": issue_number,
        "at": now.astimezone(UTC).isoformat(),
        "ttl": ttl,
    }
    return CLAIM_PREFIX + json.dumps(payload, sort_keys=True, separators=(",", ":"))


def release_body(
    worker_id: str,
    issue_number: int,
    now: datetime,
    claim_id: int,
    reason: str = "released",
) -> str:
    """释放必须指向一条具体的 claim comment id。

    按 worker 名释放会跨轮误伤：这个 worker 上一轮释放过，下一轮的新 claim
    也会被那条旧 release 一并作废。
    """

    payload = {
        "v": 2,
        "worker": worker_id,
        "issue": issue_number,
        "at": now.astimezone(UTC).isoformat(),
        "claim_id": claim_id,
        "reason": reason,
    }
    return RELEASE_PREFIX + json.dumps(payload, sort_keys=True, separators=(",", ":"))


def parse_events(comments: list[dict]) -> tuple[list[Claim], set[int]]:
    """把 comment 列表解析成 (claim 列表, 已释放/撤回的 claim id 集合)。

    畸形或版本不符的行一律跳过：解析不出来就当作没有这条 claim，
    绝不能靠猜去补一个「看起来合理」的认领。没有 claim_id 的旧格式
    release 同样忽略，不能猜它想释放谁。
    """

    claims: list[Claim] = []
    released_ids: set[int] = set()
    for comment in comments:
        body = comment.get("body") or ""
        try:
            if body.startswith(CLAIM_PREFIX):
                payload = json.loads(body[len(CLAIM_PREFIX) :])
                if payload.get("v") != 1:
                    continue
                claims.append(
                    Claim(
                        comment_id=int(comment["id"]),
                        worker_id=str(payload["worker"]),
                        created_at=_as_utc(datetime.fromisoformat(payload["at"])),
                        ttl_seconds=int(payload.get("ttl", DEFAULT_TTL_SECONDS)),
                    )
                )
            elif body.startswith(RELEASE_PREFIX):
                payload = json.loads(body[len(RELEASE_PREFIX) :])
                claim_id = payload.get("claim_id")
                if isinstance(claim_id, int):
                    released_ids.add(claim_id)
        except (ValueError, KeyError, TypeError):
            continue
    return claims, released_ids


def arbitrate(
    claims: list[Claim],
    worker_id: str,
    now: datetime,
    *,
    released_ids: set[int] | None = None,
    has_open_pr: bool = False,
) -> tuple[bool, Claim | None, str]:
    """有效 claim 中 id 最小者获准。返回 (是否获准, 胜者, 原因)。

    过期的 claim 视为失效（worker 崩溃后不会永久占位）；但该 Issue 已有
    open PR 时把有效期放宽 PR_TTL_MULTIPLIER 倍，避免抢占正在开发的分支，
    同时保留上界，防止一条悬空 PR 把 Issue 永久锁死。
    """

    released_ids = released_ids or set()
    now = _as_utc(now)
    active = []
    for claim in claims:
        if claim.comment_id in released_ids:
            continue
        multiplier = PR_TTL_MULTIPLIER if has_open_pr else 1
        if now - _as_utc(claim.created_at) > timedelta(seconds=claim.ttl_seconds * multiplier):
            continue
        active.append(claim)
    if not active:
        return False, None, "no_active_claim"
    winner = min(active, key=lambda item: item.comment_id)
    if winner.worker_id != worker_id:
        return False, winner, "lost_arbitration"
    return True, winner, "granted"


def active_claims(
    claims: list[Claim], now: datetime, *, released_ids: set[int], has_open_pr: bool
) -> list[Claim]:
    """给 list / next 用：还有哪些 claim 仍在占着这个 Issue。"""

    result = []
    multiplier = PR_TTL_MULTIPLIER if has_open_pr else 1
    for claim in claims:
        if claim.comment_id in released_ids:
            continue
        if _as_utc(now) - _as_utc(claim.created_at) > timedelta(
            seconds=claim.ttl_seconds * multiplier
        ):
            continue
        result.append(claim)
    return result


# ---------------------------------------------------------------------------
# GitHub API 客户端
# ---------------------------------------------------------------------------


def github_token() -> str:
    from_env = os.environ.get("GITHUB_TOKEN", "").strip()
    if from_env:
        return from_env
    completed = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True,
        text=True,
        check=True,
    )
    for line in completed.stdout.splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1]
    raise RuntimeError("未取到 GitHub 凭据：请设置 GITHUB_TOKEN 或配置 git credential manager")


class GitHubClient:
    def __init__(self, repo: str, token: str) -> None:
        self.repo = repo
        self.token = token
        # 本轮实测 GitHub API 直连可用、代理返回空；直连优先，失败再尝试代理。
        self._openers = [
            urllib.request.build_opener(urllib.request.ProxyHandler({})),
            urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": PROXY, "https": PROXY})
            ),
        ]

    def _request(self, method: str, path: str, payload: dict | None = None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        last_error: Exception | None = None
        for opener in self._openers:
            request = urllib.request.Request(
                API_ROOT + path,
                data=data,
                method=method,
                headers={
                    "Authorization": "Bearer " + self.token,
                    "Accept": "application/vnd.github+json",
                    "Content-Type": "application/json",
                    "User-Agent": "workpanda-issue-claim",
                },
            )
            try:
                with opener.open(request, timeout=25) as response:
                    return json.loads(response.read().decode("utf-8"))
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
        raise RuntimeError(f"GitHub API {method} {path} 失败：{last_error}")

    def list_open_issues(self) -> list[dict]:
        return self._request("GET", f"/repos/{self.repo}/issues?state=open&per_page=100")

    def get_issue(self, number: int) -> dict:
        return self._request("GET", f"/repos/{self.repo}/issues/{number}")

    def list_comments(self, number: int) -> list[dict]:
        return self._request("GET", f"/repos/{self.repo}/issues/{number}/comments?per_page=100")

    def add_comment(self, number: int, body: str) -> dict:
        return self._request("POST", f"/repos/{self.repo}/issues/{number}/comments", {"body": body})

    def list_branches(self) -> list[dict]:
        return self._request("GET", f"/repos/{self.repo}/branches?per_page=100")

    def list_open_pulls(self) -> list[dict]:
        return self._request("GET", f"/repos/{self.repo}/pulls?state=open&per_page=100")

    def list_recent_commits(self) -> list[dict]:
        return self._request("GET", f"/repos/{self.repo}/commits?per_page=50")

    def check_runs(self, sha: str) -> list[dict]:
        result = self._request("GET", f"/repos/{self.repo}/commits/{sha}/check-runs")
        return result.get("check_runs", [])


# ---------------------------------------------------------------------------
# 锁与业务动作
# ---------------------------------------------------------------------------


def lock_path() -> Path:
    configured = os.environ.get("WORKPANDA_CLAIM_LOCK", "").strip()
    if configured:
        return Path(configured)
    # 必须放在仓库之外：共享 worktree 会被切分支、commit -a 影响。
    return Path.home() / ".workbuddy" / "locks" / "workpanda-issue-claim.lock"


@contextmanager
def claim_lock():
    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def require_worker() -> str:
    worker = os.environ.get("WORKPANDA_WORKER_ID", "").strip()
    if not worker:
        raise RuntimeError("必须设置 WORKPANDA_WORKER_ID（每个 worker 唯一）")
    return worker


def ttl_seconds() -> int:
    raw = os.environ.get("WORKPANDA_CLAIM_TTL_SECONDS", "").strip()
    return int(raw) if raw else DEFAULT_TTL_SECONDS


def branch_prefix(number: int) -> str:
    return f"issue/{number}-"


def _has_open_pr(pulls: list[dict], number: int) -> bool:
    prefix = branch_prefix(number)
    return any((pr.get("head", {}).get("ref") or "").startswith(prefix) for pr in pulls)


def _withdraw(
    client, number: int, worker_id: str, now: datetime, claim_id: int, reason: str
) -> None:
    """判输者立刻撤回自己的 claim，不能留下一条「从没开工却占着位置」的记录。"""

    client.add_comment(number, release_body(worker_id, number, now, claim_id, reason))


def claim_once(client, number: int, worker_id: str, now: datetime, ttl: int) -> dict:
    """发 claim comment → 回读全部 → 最小 id 判定 → 再回读确认。"""

    issue = client.get_issue(number)

    def refused(reason: str) -> dict:
        return {"granted": False, "issue": number, "worker_id": worker_id, "reason": reason}

    if issue.get("state") != "open":
        return refused("issue_not_open")
    if "pull_request" in issue:
        return refused("is_pull_request")

    created = client.add_comment(number, claim_body(worker_id, number, now, ttl))
    claim_id = int(created["id"])
    comments = client.list_comments(number)
    claims, released_ids = parse_events(comments)
    has_pr = _has_open_pr(client.list_open_pulls(), number)
    granted, winner, reason = arbitrate(
        claims, worker_id, now, released_ids=released_ids, has_open_pr=has_pr
    )

    result = {
        "granted": granted,
        "issue": number,
        "worker_id": worker_id,
        "claim_comment_id": claim_id,
        "winner_comment_id": winner.comment_id if winner else None,
        "reason": reason,
    }
    if not granted:
        _withdraw(client, number, worker_id, now, claim_id, "lost_arbitration")
        return result

    # 写后读回：判定必须基于服务端最终状态，不能拿刚发出去的那条当结论。
    again, released_again = parse_events(client.list_comments(number))
    still, _, _ = arbitrate(
        again, worker_id, now, released_ids=released_again, has_open_pr=has_pr
    )
    if not still:
        result["granted"] = False
        result["reason"] = "lost_after_readback"
        _withdraw(client, number, worker_id, now, claim_id, "lost_after_readback")
        return result

    result["branch"] = branch_prefix(number) + worker_id
    result["worktree"] = str(Path.home() / ".workbuddy" / "wt" / result["branch"].replace("/", "-"))
    return result


def release_claim(client, number: int, worker_id: str, now: datetime) -> dict:
    """只释放自己当前获准的那一条 claim，不误伤历史轮次。"""

    comments = client.list_comments(number)
    claims, released_ids = parse_events(comments)
    has_pr = _has_open_pr(client.list_open_pulls(), number)
    granted, winner, reason = arbitrate(
        claims, worker_id, now, released_ids=released_ids, has_open_pr=has_pr
    )
    if not granted or winner is None:
        return {
            "ok": False,
            "issue": number,
            "worker_id": worker_id,
            "reason": "no_active_claim_to_release: " + reason,
        }
    client.add_comment(number, release_body(worker_id, number, now, winner.comment_id))
    return {
        "ok": True,
        "issue": number,
        "worker_id": worker_id,
        "released_claim_id": winner.comment_id,
    }


def eligible_issues(client, now: datetime, ttl: int) -> list[int]:
    """只自动领 enhancement 且无人占用的 Issue。

    question / documentation 类（决策、总览）不自动领取 —— 对应开发机制里
    「高风险任务不能由 AI 自行领取」。
    """

    issues = client.list_open_issues()
    branches = [item.get("name", "") for item in client.list_branches()]
    pulls = client.list_open_pulls()
    result: list[int] = []
    for issue in sorted(issues, key=lambda item: item["number"]):
        number = issue["number"]
        if "pull_request" in issue:
            continue
        labels = {label.get("name") for label in issue.get("labels", [])}
        if ELIGIBLE_LABEL not in labels:
            continue
        if issue.get("assignees"):
            continue
        if any(name.startswith(branch_prefix(number)) for name in branches):
            continue
        claims, released_ids = parse_events(client.list_comments(number))
        if active_claims(
            claims, now, released_ids=released_ids, has_open_pr=_has_open_pr(pulls, number)
        ):
            continue
        result.append(number)
    return result


def guard(client, number: int, worker_id: str, sha: str, now: datetime) -> dict:
    """L3：合并前最后一道不依赖「大家都守规矩」的闸门。"""

    def reject(reason: str) -> dict:
        return {"ok": False, "issue": number, "worker_id": worker_id, "reason": reason}

    issue = client.get_issue(number)
    if issue.get("state") != "open":
        return reject("issue_not_open")

    comments = client.list_comments(number)
    claims, released_ids = parse_events(comments)
    pulls = client.list_open_pulls()
    has_pr = _has_open_pr(pulls, number)
    granted, _, reason = arbitrate(
        claims, worker_id, now, released_ids=released_ids, has_open_pr=has_pr
    )
    if not granted:
        return reject("claim_lost_or_expired: " + reason)

    mine = branch_prefix(number) + worker_id
    competitors = [
        pr.get("head", {}).get("ref")
        for pr in pulls
        if (pr.get("head", {}).get("ref") or "").startswith(branch_prefix(number))
        and pr.get("head", {}).get("ref") != mine
    ]
    if competitors:
        return reject("competing_branch: " + ", ".join(str(item) for item in competitors))

    pattern = re.compile(rf"(?:Closes|Fixes|Resolves)\s+#{number}\b", re.IGNORECASE)
    for commit in client.list_recent_commits():
        message = commit.get("commit", {}).get("message", "")
        if pattern.search(message):
            return reject("already_merged")

    runs = client.check_runs(sha)
    if not runs:
        return reject("ci_not_reported")
    if not all(
        run.get("status") == "completed" and run.get("conclusion") == "success" for run in runs
    ):
        return reject("ci_not_green")
    return {"ok": True, "issue": number, "worker_id": worker_id, "sha": sha, "checks": len(runs)}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def head_sha(repo: str | None) -> str:
    command = ["git"]
    if repo:
        command += ["-C", repo]
    command += ["rev-parse", "HEAD"]
    return subprocess.run(command, capture_output=True, text=True, check=True).stdout.strip()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="WorkPanda 多 worker Issue 认领协议")
    parser.add_argument("--repo", default=os.environ.get("WORKPANDA_REPO", DEFAULT_REPO))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="列出可自动认领的 Issue（只读）")
    sub.add_parser("next", help="领下一个可认领的 Issue")
    claim = sub.add_parser("claim", help="领指定 Issue")
    claim.add_argument("issue", type=int)
    release = sub.add_parser("release", help="释放自己的认领")
    release.add_argument("issue", type=int)
    check = sub.add_parser("guard", help="合并前复核")
    check.add_argument("issue", type=int)
    check.add_argument("--sha", default=None, help="待合并提交的 sha，默认取当前 HEAD")
    check.add_argument("--git-dir", default=None, help="取 HEAD 时使用的仓库路径")
    return parser


def run(args) -> tuple[dict, int]:
    now = utcnow()
    ttl = ttl_seconds()
    client = GitHubClient(args.repo, github_token())

    if args.command == "list":
        return {"ok": True, "eligible": eligible_issues(client, now, ttl)}, 0

    worker_id = require_worker()
    with claim_lock():
        if args.command == "next":
            candidates = eligible_issues(client, now, ttl)
            if not candidates:
                return {
                    "granted": False,
                    "worker_id": worker_id,
                    "reason": "no_eligible_issue",
                }, 2
            result = claim_once(client, candidates[0], worker_id, now, ttl)
            return result, 0 if result["granted"] else 2

        if args.command == "claim":
            result = claim_once(client, args.issue, worker_id, now, ttl)
            return result, 0 if result["granted"] else 2

        if args.command == "release":
            result = release_claim(client, args.issue, worker_id, now)
            return result, 0 if result["ok"] else 3

        if args.command == "guard":
            sha = args.sha or head_sha(args.git_dir)
            result = guard(client, args.issue, worker_id, sha, now)
            return result, 0 if result["ok"] else 3

    raise RuntimeError(f"未知子命令：{args.command}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload, code = run(args)
    except Exception as exc:  # noqa: BLE001 - CLI 需要把任何失败都变成可解析的 JSON
        error = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        print(json.dumps(error, ensure_ascii=False))
        return 4
    print(json.dumps(payload, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
