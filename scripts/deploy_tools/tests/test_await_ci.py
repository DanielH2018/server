#!/usr/bin/env python3
"""Tests for await_ci.py -- the master-CI wait a session runs after a merge.

Every rule here is an accept/reject pair. A gate that is only ever observed passing is
indistinguishable from one that cannot go red, and this one decides whether a deploy
proceeds -- so each case that must pass has a sibling that must fail.

Run: uv run pytest scripts/deploy_tools/tests/test_await_ci.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import await_ci

REQUIRED = frozenset({"prek (lint + validate + tests + secrets)"})


def _run(name, status, conclusion):
    return {"name": name, "status": status, "conclusion": conclusion}


def test_green_sha_is_clean():
    runs = [_run("prek (lint + validate + tests + secrets)", "completed", "success")]
    assert await_ci.verdict_for("abc", lambda _s: runs, REQUIRED) == "pass"


def test_red_sha_is_flagged():
    runs = [_run("prek (lint + validate + tests + secrets)", "completed", "failure")]
    assert await_ci.verdict_for("abc", lambda _s: runs, REQUIRED) == "fail"


def test_empty_check_run_list_is_pending_never_green():
    """A freshly-pushed merge commit has no runs registered yet.

    Reading that as green is how a session deploys ahead of the CI it believes it waited for.
    """
    assert await_ci.verdict_for("abc", lambda _s: [], REQUIRED) == "pending"


def test_cancelled_alone_is_pending_not_fail():
    runs = [_run("prek (lint + validate + tests + secrets)", "completed", "cancelled")]
    assert await_ci.verdict_for("abc", lambda _s: runs, REQUIRED) == "pending"


def test_unreachable_api_is_pending_not_pass():
    """The gate fails closed or it is not a gate."""

    def boom(_sha):
        raise TimeoutError("api unreachable")

    assert await_ci.verdict_for("abc", boom, REQUIRED) == "pending"


def test_no_verdict_sha_resolves_to_the_tip_when_it_is_an_ancestor():
    resolved = await_ci.resolve_sha(
        "aaa",
        fetch_tip=lambda: "bbb",
        is_ancestor=lambda a, b: (a, b) == ("aaa", "bbb"),
    )
    assert resolved == "bbb"


def test_no_verdict_sha_does_not_resolve_to_an_unrelated_tip():
    """The reject half: an unrelated tip says nothing about this commit.

    Following it would report somebody else's verdict as this PR's.
    """
    resolved = await_ci.resolve_sha(
        "aaa", fetch_tip=lambda: "ccc", is_ancestor=lambda _a, _b: False
    )
    assert resolved == "aaa"


def test_empty_required_set_refuses_rather_than_disarming():
    """ci_verdict returns 'pass' on an empty required set -- that is the deployer's
    deliberate disarm switch (deploy_logic.py:366), reachable only by an operator
    emptying CI_CONTEXTS. Inheriting it here would turn a wait-for-green into an
    unconditional green."""
    try:
        await_ci.verdict_for("abc", lambda _s: [], frozenset())
    except await_ci.DisarmedGateError:
        return
    raise AssertionError("an empty required set must raise, not return pass")


def test_only_no_verdict_conclusions_is_detected():
    runs = [_run("prek (lint + validate + tests + secrets)", "completed", "cancelled")]
    assert await_ci._has_only_no_verdict_conclusions(runs, REQUIRED)


def test_a_real_failure_is_not_read_as_no_verdict():
    """The reject half of the tip-following trigger.

    A genuine failure must stop the wait, not send it chasing the tip for a second opinion.
    """
    runs = [_run("prek (lint + validate + tests + secrets)", "completed", "failure")]
    assert not await_ci._has_only_no_verdict_conclusions(runs, REQUIRED)


# --- the token the poll authenticates with -------------------------------------------------
#
# The anonymous limit is 60/hour PER SOURCE IP and shared with the deployer's own gate on the
# same host. At one poll per 20s for up to 900s, one landing costs 45 of those 60, so the second
# landing in an hour starved the tick into `HTTP Error 403: rate limit exceeded` (2026-09-01).
# Each rule is an accept/reject pair: a resolver that always returned a token and one that
# never did are indistinguishable from the passing side alone.


class _Proc:
    def __init__(self, returncode, stdout):
        self.returncode = returncode
        self.stdout = stdout


def test_env_token_wins_without_running_gh():
    def never(*_a, **_k):
        raise AssertionError("gh must not run when the environment carries a token")

    assert await_ci.github_token({"GH_TOKEN": "ghp_env"}, never) == "ghp_env"


def test_gh_auth_token_is_used_when_the_environment_has_none():
    calls = []

    def run(cmd, **_k):
        calls.append(cmd)
        return _Proc(0, "gho_cli\n")

    assert await_ci.github_token({}, run) == "gho_cli"
    assert calls == [["gh", "auth", "token"]]


def test_no_token_falls_back_to_anonymous():
    """The reject half: no token must fall back to anonymous, never to a crash.

    A logged-out gh, a missing binary, a hung keyring -- every one of them must degrade to the
    anonymous request this poll made before.
    """
    assert await_ci.github_token({}, lambda *_a, **_k: _Proc(1, "")) is None
    assert await_ci.github_token({}, lambda *_a, **_k: _Proc(0, "  \n")) is None

    def boom(*_a, **_k):
        raise FileNotFoundError("gh")

    assert await_ci.github_token({}, boom) is None
    assert await_ci.github_token({"GH_TOKEN": "   "}, boom) is None


def test_auth_header_is_bearer_or_absent():
    assert await_ci.github_auth_headers("tok") == {"Authorization": "Bearer tok"}
    assert await_ci.github_auth_headers(None) == {}
    assert await_ci.github_auth_headers("") == {}


def _suite(status, conclusion, runs):
    return {"status": status, "conclusion": conclusion, "latest_check_runs_count": runs}


def test_a_workflow_cancelled_before_any_run_registered_is_no_verdict():
    """#766 on 2026-09-02: a workflow cancelled before any run registered is no verdict.

    The merge commit's CI suite read `completed cancelled` with zero check-runs, so the required
    name never appeared and two waits sat out 2400s.
    """
    suites = [_suite("completed", "success", 2), _suite("completed", "cancelled", 0)]
    assert await_ci._cancelled_before_any_run_registered([], suites, REQUIRED)


def test_a_freshly_pushed_sha_with_no_suites_keeps_waiting():
    assert not await_ci._cancelled_before_any_run_registered([], [], REQUIRED)


def test_a_suite_still_queued_keeps_waiting():
    """A queued suite is a run that may yet register; chasing the tip here would report
    somebody else's result as this commit's."""
    suites = [_suite("queued", None, 0)]
    assert not await_ci._cancelled_before_any_run_registered([], suites, REQUIRED)


def test_a_registered_required_run_is_never_read_as_cancelled_before_registration():
    runs = [_run("prek (lint + validate + tests + secrets)", "in_progress", None)]
    suites = [_suite("completed", "cancelled", 0)]
    assert not await_ci._cancelled_before_any_run_registered(runs, suites, REQUIRED)


def test_wait_follows_the_tip_when_the_workflow_was_cancelled_before_registering(
    monkeypatch,
):
    """End to end through wait(): the cancelled SHA has no runs, the tip is green."""
    green = [_run("prek (lint + validate + tests + secrets)", "completed", "success")]
    runs_by_sha = {"old": [], "tip": green}
    suites_by_sha = {"old": [_suite("completed", "cancelled", 0)], "tip": []}
    monkeypatch.setattr(
        await_ci, "resolve_sha", lambda sha: "tip" if sha == "old" else sha
    )
    monkeypatch.setattr(await_ci, "required_contexts", lambda: REQUIRED)
    code, msg = await_ci.wait(
        "old",
        60,
        1,
        sleep=lambda _s: None,
        clock=lambda: 0,
        fetch=lambda s: runs_by_sha[s],
        fetch_suites=lambda s: suites_by_sha[s],
    )
    assert (code, msg) == (0, "tip: CI green")


def test_wait_does_not_follow_the_tip_for_a_fresh_sha(monkeypatch):
    """The reject half: a fresh SHA must not send the wait chasing the tip.

    No suites at all means the run has not registered; the wait must stay on the SHA it was given
    and run out its budget rather than chase the tip.
    """
    green = [_run("prek (lint + validate + tests + secrets)", "completed", "success")]
    runs_by_sha = {"old": [], "tip": green}
    ticks = iter([0, 0, 100])
    monkeypatch.setattr(
        await_ci, "resolve_sha", lambda sha: "tip" if sha == "old" else sha
    )
    monkeypatch.setattr(await_ci, "required_contexts", lambda: REQUIRED)
    code, msg = await_ci.wait(
        "old",
        60,
        1,
        sleep=lambda _s: None,
        clock=lambda: next(ticks),
        fetch=lambda s: runs_by_sha[s],
        fetch_suites=lambda s: [],
    )
    assert code == 75
    assert msg.startswith("old:")
