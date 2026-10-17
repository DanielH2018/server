#!/usr/bin/env python3
"""Tests for await_ci.py -- the master-CI wait a session runs after a merge.

Every rule here is an accept/reject pair. A gate that is only ever observed passing is
indistinguishable from one that cannot go red, and this one decides whether a deploy
proceeds -- so each case that must pass has a sibling that must fail.

Run: uv run pytest scripts/deploy_tools/tests/test_await_ci.py
"""

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


def _suite(status, conclusion, runs):
    return {"status": status, "conclusion": conclusion, "latest_check_runs_count": runs}


def test_a_workflow_cancelled_before_any_run_registered_is_no_verdict():
    """A workflow cancelled before any run registered is no verdict.

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


def _actions_suite(status):
    return {"app": {"slug": "github-actions"}, "status": status}


def test_a_sha_with_no_actions_suite_reads_as_a_dropped_push():
    """#4621: GitHub delivered no push event, so no app registered anything at all."""
    assert await_ci._push_was_dropped([], [], REQUIRED)
    other_apps = [{"app": {"slug": "gitguardian"}, "status": "queued"}]
    assert await_ci._push_was_dropped([], other_apps, REQUIRED)


def test_a_queued_actions_suite_is_not_a_dropped_push():
    assert not await_ci._push_was_dropped([], [_actions_suite("queued")], REQUIRED)


def test_descendant_verdict_skips_a_pending_commit_for_a_green_one():
    green = [_run("prek (lint + validate + tests + secrets)", "completed", "success")]
    runs_by_sha = {"near": [], "middle": green, "tip": green}
    found = await_ci.descendant_verdict(
        "old",
        fetch=lambda s: runs_by_sha[s],
        descendants=lambda _s: ["near", "middle", "tip"],
    )
    assert found == ("middle", "pass")


def test_descendant_verdict_stops_at_a_red_commit_before_a_green_one():
    """The reject half: a later green commit may carry the fix the merge commit lacks."""
    red = [_run("prek (lint + validate + tests + secrets)", "completed", "failure")]
    green = [_run("prek (lint + validate + tests + secrets)", "completed", "success")]
    runs_by_sha = {"near": [], "broken": red, "fixed": green}
    found = await_ci.descendant_verdict(
        "old",
        fetch=lambda s: runs_by_sha[s],
        descendants=lambda _s: ["near", "broken", "fixed"],
    )
    assert found == ("broken", "fail")


def _wait_on_dropped(suites, descendant_runs, late_runs=None):
    """wait() on a SHA with no runs, past the grace, with one descendant `next`.

    REQUIRED is await_ci.REQUIRED_CONTEXTS, so nothing needs patching.
    """
    runs_by_sha = {"old": [], "next": descendant_runs}
    now = [0]

    def fetch(sha):
        # The descendant goes green one poll after the grace; a late run lands before it.
        if now[0] <= await_ci.DROPPED_PUSH_GRACE_S + 20:
            return []
        if sha == "old" and late_runs:
            return late_runs
        return runs_by_sha[sha]

    def sleep(seconds):
        now[0] += seconds

    return await_ci.wait(
        "old",
        await_ci.DROPPED_PUSH_GRACE_S * 2,
        20,
        sleep=sleep,
        clock=lambda: now[0],
        fetch=fetch,
        fetch_suites=lambda _s: suites,
        descendants=lambda _s: ["next"],
    )


def test_wait_takes_a_green_descendant_of_a_dropped_push():
    green = [_run("prek (lint + validate + tests + secrets)", "completed", "success")]
    code, msg = _wait_on_dropped([], green)
    assert code == 0
    assert msg.startswith("next: CI green")


def test_wait_does_not_take_a_descendant_while_an_actions_suite_is_queued():
    """The reject half: a registered CI run is this SHA's own verdict, still on its way."""
    green = [_run("prek (lint + validate + tests + secrets)", "completed", "success")]
    code, msg = _wait_on_dropped([_actions_suite("queued")], green)
    assert code == 75
    assert msg.startswith("old:") and "never ran CI" not in msg


def test_a_dropped_push_whose_descendant_ci_is_in_flight_times_out_saying_why():
    """The reject half of CI_UNPROVEN (#4641): a run on its way makes a re-run converge."""
    running = [_run("prek (lint + validate + tests + secrets)", "in_progress", None)]
    code, msg = _wait_on_dropped([], running)
    assert code == 75
    assert "GitHub never ran CI" in msg and "re-run converges" in msg


def test_a_late_push_whose_run_registers_after_the_grace_keeps_its_own_verdict():
    """The reject half: a run arriving after the grace is a late push, not a dropped one."""
    green = [_run("prek (lint + validate + tests + secrets)", "completed", "success")]
    running = [_run("prek (lint + validate + tests + secrets)", "in_progress", None)]
    code, msg = _wait_on_dropped([], green, late_runs=running)
    assert code == 75
    assert msg.startswith("old:") and "never ran CI" not in msg


def test_wait_on_a_dropped_push_ends_red_when_its_nearest_descendant_is_red():
    red = [_run("prek (lint + validate + tests + secrets)", "completed", "failure")]
    code, msg = _wait_on_dropped([], red)
    assert code == 1
    assert msg.startswith("next: CI RED") and "never ran CI for old" in msg


def _run_less_tip(chain, suites=(), tip="tip"):
    return await_ci.run_less_tip(
        "old",
        fetch=lambda _s: [],
        fetch_suites=lambda _s: list(suites),
        descendants=lambda _s: chain,
        fetch_tip=lambda: tip,
    )


def test_run_less_tip_names_the_tip_when_no_commit_from_the_sha_has_ci():
    assert _run_less_tip(["next", "tip"]) == "tip"
    assert _run_less_tip([], tip="old") == "old"


def test_run_less_tip_says_nothing_for_a_sha_not_on_master():
    """The reject half: no descendants because `old` is off master, not because it is the tip."""
    assert _run_less_tip([], tip="other") == ""


def test_run_less_tip_says_nothing_while_a_descendant_suite_is_queued():
    assert _run_less_tip(["next"], suites=[_actions_suite("queued")]) == ""


def test_run_less_tip_says_nothing_past_the_descendant_window():
    chain = [f"c{i}" for i in range(await_ci.DESCENDANT_LIMIT + 1)]
    assert _run_less_tip(chain) == ""


def _wait_past_a_red_descendant(later_goes_green_at):
    """wait() on dropped `old`: `red` fails at once; `fix` is in flight until a time, or forever."""
    name = "prek (lint + validate + tests + secrets)"
    now = [0]

    def fetch(sha):
        if sha == "red":
            return [_run(name, "completed", "failure")]
        if sha == "fix":
            if later_goes_green_at is not None and now[0] >= later_goes_green_at:
                return [_run(name, "completed", "success")]
            return [_run(name, "in_progress", None)]
        return []

    def sleep(seconds):
        now[0] += seconds

    return await_ci.wait(
        "old",
        900,
        20,
        sleep=sleep,
        clock=lambda: now[0],
        fetch=fetch,
        fetch_suites=lambda _s: [],
        descendants=lambda _s: ["red", "fix"],
        fetch_tip=lambda: "fix",
    )


def test_wait_keeps_polling_past_a_red_descendant_while_a_later_one_is_in_flight():
    """#4649 at merge time: the fix's CI is still running when the red one is found."""
    code, msg = _wait_past_a_red_descendant(later_goes_green_at=600)
    assert code == await_ci.CI_UNPROVEN, msg
    assert "fix" in msg and "deploy.sh" in msg


def test_a_red_descendant_whose_later_one_never_settles_ends_red_saying_why():
    """The reject half: the budget runs out, and the line says what a re-run would need."""
    code, msg = _wait_past_a_red_descendant(later_goes_green_at=None)
    assert code == 1
    assert msg.startswith("red: CI RED") and "fix after it has no verdict yet" in msg


def _wait_past_reds(runs_by_sha, suites_by_sha, chain):
    """wait() on dropped `m` over a fixed descendant `chain`; returns (code, msg, elapsed)."""
    now = [0]

    def sleep(seconds):
        now[0] += seconds

    code, msg = await_ci.wait(
        "m",
        900,
        20,
        sleep=sleep,
        clock=lambda: now[0],
        fetch=lambda s: runs_by_sha.get(s, []),
        fetch_suites=lambda s: suites_by_sha.get(s, []),
        descendants=lambda _s: chain,
        fetch_tip=lambda: chain[-1],
    )
    return code, msg, now[0]


def test_a_cancelled_only_descendant_between_reds_does_not_hold_the_wait_to_its_deadline():
    """#4667 Verify-by: D3's push cancels D2's run, so D2 never gets a verdict."""
    name = "prek (lint + validate + tests + secrets)"
    red = [_run(name, "completed", "failure")]
    runs_by_sha = {"d1": red, "d2": [_run(name, "completed", "cancelled")], "d3": red}
    suites_by_sha = {
        "d1": [_actions_suite("completed")],
        "d2": [_actions_suite("completed")],
        "d3": [_actions_suite("completed")],
    }
    code, msg, elapsed = _wait_past_reds(runs_by_sha, suites_by_sha, ["d1", "d2", "d3"])
    assert code == await_ci.CI_RED, msg
    assert elapsed < await_ci.DROPPED_PUSH_GRACE_S + 20, (elapsed, msg)


def test_a_dropped_push_descendant_between_reds_does_not_hold_the_wait_to_its_deadline():
    """#4667: D2's push was dropped too, so it has no run and no Actions suite for good."""
    red = [_run("prek (lint + validate + tests + secrets)", "completed", "failure")]
    runs_by_sha = {"d1": red, "d3": red}
    suites_by_sha = {
        "d1": [_actions_suite("completed")],
        "d3": [_actions_suite("completed")],
    }
    code, msg, elapsed = _wait_past_reds(runs_by_sha, suites_by_sha, ["d1", "d2", "d3"])
    assert code == await_ci.CI_RED, msg
    assert elapsed < await_ci.DROPPED_PUSH_GRACE_S + 20, (elapsed, msg)


def test_a_run_less_tip_after_a_red_descendant_offers_the_dispatch_route():
    """#4667: D2 is the master tip and has no CI run, so only dispatching CI converges."""
    red = [_run("prek (lint + validate + tests + secrets)", "completed", "failure")]
    code, msg, _elapsed = _wait_past_reds(
        {"d1": red}, {"d1": [_actions_suite("completed")]}, ["d1", "d2"]
    )
    assert code != 75, msg
    assert "gh workflow run ci.yml --ref master" in msg, msg


def test_a_run_less_tip_after_a_red_descendant_ends_ci_unproven():
    """#4667: dispatching CI is the way out, so the wait ends on CI_UNPROVEN, not CI_RED."""
    red = [_run("prek (lint + validate + tests + secrets)", "completed", "failure")]
    code, msg, _elapsed = _wait_past_reds(
        {"d1": red}, {"d1": [_actions_suite("completed")]}, ["d1", "d2"]
    )
    assert code == await_ci.CI_UNPROVEN, msg


def test_a_tip_with_an_actions_suite_after_a_red_descendant_ends_red_at_the_deadline():
    """The reject half: a tip whose CI is queued may still settle, so it is no dispatch case."""
    red = [_run("prek (lint + validate + tests + secrets)", "completed", "failure")]
    code, msg, _elapsed = _wait_past_reds(
        {"d1": red},
        {"d1": [_actions_suite("completed")], "d2": [_actions_suite("queued")]},
        ["d1", "d2"],
    )
    assert code == await_ci.CI_RED, msg
    assert "gh workflow run" not in msg, msg


_PASS = [_run("prek (lint + validate + tests + secrets)", "completed", "success")]
_FAIL = [_run("prek (lint + validate + tests + secrets)", "completed", "failure")]


def _newest_green(rev_list, runs_by_sha):
    """`newest_green_descendant` over a canned `rev-list` (newest first) and canned CI."""
    git_calls = []

    def fake_git(*args):
        git_calls.append(args)
        return "\n".join(rev_list)

    asked = []

    def fetch(sha):
        asked.append(sha)
        return runs_by_sha[sha]

    found = await_ci.newest_green_descendant("merge", fetch=fetch, git_out=fake_git)
    return found, asked, git_calls


def test_newest_green_descendant_takes_the_newest_green_commit_and_stops():
    found, asked, git_calls = _newest_green(
        ["tip", "mid", "old"], {"tip": [], "mid": _PASS, "old": _PASS}
    )
    assert found == "mid"
    assert asked == ["tip", "mid"]
    assert git_calls == [("rev-list", "--first-parent", "merge..origin/master")]


def test_newest_green_descendant_offers_the_tip():
    """`ci_walk_candidates` drops the tip; this caller holds no verdict for it."""
    found, _, _ = _newest_green(["tip", "old"], {"tip": _PASS, "old": _PASS})
    assert found == "tip"


def test_newest_green_descendant_returns_nothing_when_none_is_green():
    found, _, _ = _newest_green(["tip", "old"], {"tip": [], "old": []})
    assert found == ""


def test_a_red_commit_ends_the_walk_before_an_older_green_one():
    """The `ci-unproven` case: a green on the far side of a red says nothing about it."""
    found, asked, _ = _newest_green(
        ["tip", "red", "green"], {"tip": [], "red": _FAIL, "green": _PASS}
    )
    assert found == ""
    assert asked == ["tip", "red"]


def test_newest_green_descendant_reads_at_most_the_descendant_limit():
    shas = [f"c{n}" for n in range(await_ci.DESCENDANT_LIMIT + 5)]
    found, asked, _ = _newest_green(shas, {s: [] for s in shas})
    assert found == "" and asked == shas[: await_ci.DESCENDANT_LIMIT]
