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

import await_ci  # noqa: E402 — needs the path insert above

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
    """A freshly-pushed merge commit has no runs registered yet. Reading that as green is
    how a session deploys ahead of the CI it believes it waited for."""
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
    """The reject half: an unrelated tip says nothing about this commit, so following it
    would report somebody else's verdict as this PR's."""
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
    """The reject half of the tip-following trigger: a genuine failure must stop the wait,
    not send it chasing the tip for a second opinion."""
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
    """The reject half: a logged-out gh, a missing binary, a hung keyring -- every one of
    them must degrade to the anonymous request this poll made before, never to a crash."""
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
