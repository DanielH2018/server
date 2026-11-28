"""--await-merge takes the landing turn before it merges, when its head can use it.

Run: uv run pytest scripts/deploy_tools/tests/land_merge/test_land_turn.py
"""

import subprocess
from dataclasses import replace

import pytest

from _land_fakes import Fakes
from _land_turn_fakes import (
    A_RUN,
    HEAD,
    MASTER_MOVED,
    MERGED,
    OPEN,
    START_VIEW,
    UPDATED,
    first_gh,
    land_in_turn,
    line_calls,
    merged_sha,
    call_order,
    turn_landing,
)
from deploy_tools.land_lib import line, stale_base, turn
from deploy_tools.land_lib.outcome import Outcome, Verdict
from lib.exit_codes import CI_RED
from lib.proc_testing import fake_bin, path_with


def test_a_landing_waits_while_another_holds_the_turn(landing, capsys):
    # fact: .claude/skills/renovate-prs/SKILL.md#6. Land them one at a time
    ln, _ = turn_landing(landing, turn=[False, False, True], merge_poll=7)
    land_in_turn(ln)
    out = capsys.readouterr().out
    assert out.count("waiting for the landing turn:") == 1
    assert "took the landing turn after 14s" in out
    assert "merged after 14s, 14s of it waiting for the landing turn" in out


def test_a_turn_that_never_comes_leaves_a_merge_of_a_ready_pr(landing, capsys):
    ln, calls = turn_landing(landing, turn=[False], merge_timeout=2, merge_poll=1)
    land_in_turn(ln)
    order = call_order(calls)
    # The draft is still marked ready and no turn is given up.
    assert order[-2:] == ["pr ready", "merge"]
    assert "release_turn" not in order
    assert (
        "s for the landing turn, reaching merge_timeout=2s; going on without a turn"
        in capsys.readouterr().out
    )


def test_the_merge_wait_gets_a_whole_budget_after_the_wait_for_the_turn(landing):
    ln, _ = turn_landing(
        landing,
        polls=(OPEN,),
        turn=[False, False, True],
        await_ci=[(75, "pending")],
        merge_timeout=3,
        merge_poll=2,
    )
    with pytest.raises(Outcome) as exc:
        land_in_turn(ln)
    # 4s for the turn, then polls at 4s and 6s inside the budget and the end at 8s.
    assert exc.value.verdict == "merge-timeout"
    assert "still OPEN after 8s" in exc.value.error


def test_a_draft_stands_in_the_line_while_it_waits_and_leaves_with_the_turn(landing):
    ln, calls = turn_landing(landing, turn=[False, False, True])
    land_in_turn(ln)
    # Each poll stands before it tries the lock, and reads its answer again once it holds it.
    assert line_calls(calls) == [
        ("join", "999", HEAD, ""),
        ("stand", "999", HEAD, "1"),
        ("stand", "999", HEAD, "1"),
        ("stand", "999", HEAD, "1"),
        ("leave", "999"),
    ]
    # It left before it used the turn, so it is not in the line it reads as the holder.
    assert calls.index(("line", ("leave", "999"), {})) < first_gh(calls)


@pytest.mark.parametrize(
    "case",
    [
        pytest.param({"head_runs": A_RUN}, id="a-head-with-a-run"),
        pytest.param({"start": {**OPEN, "isDraft": False}}, id="a-ready-pr"),
    ],
)
def test_a_head_that_is_no_untested_draft_holds_a_place_no_holder_stacks(landing, case):
    ln, calls = turn_landing(landing, turn=[False, True], **case)
    land_in_turn(ln)
    assert call_order(calls).count("try_turn") == 2
    stood = [args for args in line_calls(calls) if args[0] != "leave"]
    assert stood and {args[2] for args in stood} == {line.NO_HEAD}


def test_a_landing_tries_the_lock_only_once_no_older_ticket_waits(landing, capsys):
    # The first two polls read #7's older ticket; the third finds #7 gone.
    ln, calls = turn_landing(landing, turn=[True], line=["\n7 0", "\n7 0", ""])
    land_in_turn(ln)
    assert call_order(calls).count("try_turn") == 1
    assert ln.ledger.turn_wait == 2 * ln.opts.merge_poll
    out = capsys.readouterr().out
    assert out.count("a landing that asked for it before this one goes first") == 1


def test_a_waiter_that_loses_the_turn_host_goes_on_without_the_turn(landing, capsys):
    ln, calls = turn_landing(
        landing,
        turn=[OSError("ssh daniel-box: no route")],
        line=["\n7 0", OSError("ssh daniel-box: no route")],
    )
    land_in_turn(ln)
    # The first poll defers. The lost line reads no ticket, so the second tries the lock,
    # which fails too, and the landing merges with no turn to give up.
    order = call_order(calls)
    assert order.count("try_turn") == 1
    assert order[-1] == "merge"
    assert "release_turn" not in order
    assert (
        "(ssh daniel-box: no route); going on without a turn" in capsys.readouterr().out
    )


def test_a_draft_the_holder_of_the_turn_merged_stops_waiting_and_merges_nothing(
    landing, capsys
):
    ln, calls = turn_landing(
        landing,
        polls=(MERGED,),
        start=(OPEN, MERGED),
        turn=[False],
        line=["", line.MERGED],
        merge_poll=7,
    )
    land_in_turn(ln)
    # No ready flip, no merge and no turn to give up: the holder did all three.
    assert call_order(calls) == ["try_turn"]
    assert line_calls(calls)[-1] == ("leave", "999")
    out = capsys.readouterr().out
    assert "the landing that holds the turn merged PR #999 with its own" in out
    assert "merged after 7s, 7s of it waiting for the landing turn" in out


@pytest.mark.parametrize(
    ("turns", "answers", "calls_made"),
    [
        pytest.param([False], ["", line.CONFLICT], ["try_turn"], id="while-it-waits"),
        pytest.param(
            [False, True],
            ["", "", line.CONFLICT],
            ["try_turn", "try_turn", "release_turn"],
            id="as-the-turn-frees",
        ),
    ],
)
def test_a_draft_the_holder_found_conflicting_ends_merge_conflict(
    landing, turns, answers, calls_made
):
    ln, calls = turn_landing(landing, turn=turns, line=answers)
    with pytest.raises(Outcome) as ended:
        land_in_turn(ln)
    assert ended.value.verdict == Verdict.MERGE_CONFLICT
    # It merges nothing, holds no turn and leaves its place.
    assert call_order(calls) == calls_made
    assert line_calls(calls)[-1] == ("leave", "999")
    assert ln.ledger.turn_wait == ln.opts.merge_poll


def test_a_draft_merged_by_the_holder_that_frees_the_turn_gives_it_straight_back(
    landing, capsys
):
    ln, calls = turn_landing(
        landing,
        polls=(MERGED,),
        start=(OPEN, MERGED),
        turn=[False, True],
        line=["", "", line.MERGED],
    )
    with turn.held(ln) as take_turn:
        assert take_turn(HEAD)[0] is False
    assert call_order(calls) == ["try_turn", "try_turn", "release_turn"]
    out = capsys.readouterr().out
    assert "the landing that holds the turn merged PR #999 with its own" in out
    assert "took the landing turn" not in out


def test_a_draft_told_to_land_alone_stands_on_with_no_head_and_its_ticket(landing):
    ln, calls = turn_landing(
        landing, turn=[False, False, False, True], line=["", line.ALONE, ""]
    )
    land_in_turn(ln)
    assert call_order(calls)[-2:] == ["merge", "release_turn"]
    # It takes its place anew at once with no head, so no holder stacks it again, and
    # keeps the ticket it first took.
    stood = line_calls(calls)
    assert stood[:3] == [
        ("join", "999", HEAD, ""),
        ("stand", "999", HEAD, "1"),
        ("join", "999", line.NO_HEAD, "1"),
    ]
    assert {args[2:] for args in stood[3:-1]} == {(line.NO_HEAD, "1")}
    assert stood[-1] == ("leave", "999")


def test_a_line_that_fails_leaves_the_landing_waiting_for_the_turn(landing, capsys):
    ln, calls = turn_landing(
        landing, turn=[False, False, True], line=[OSError("no route")]
    )
    land_in_turn(ln)
    assert call_order(calls)[-2:] == ["merge", "release_turn"]
    # One failure ends the landing's use of the line, so it is reported once.
    assert line_calls(calls) == [("join", "999", HEAD, "")]
    out = capsys.readouterr().out
    assert out.count("(no route); waiting for the turn without it") == 1


def test_a_line_that_failed_in_one_wait_is_tried_again_in_the_next(landing):
    ln, calls = turn_landing(
        landing, turn=[True], line=["", OSError("ssh daniel-box: no route"), ""]
    )
    with turn.held(ln) as take_turn:
        assert take_turn(HEAD)[0]
        ln.step_out_of_turn(lambda: None, "for a test")
    # The answer read after the first take failed; the wait after the step-out stands again
    # with the ticket the first wait took.
    assert ("join", "999", HEAD, "1") in line_calls(calls)


def test_a_draft_reads_the_line_before_it_tries_a_free_turn(landing):
    ln, calls = turn_landing(landing)
    land_in_turn(ln)
    assert calls.index(("line", ("join", "999", HEAD, ""), {})) < next(
        i for i, call in enumerate(calls) if call[0] == "try_turn"
    )
    assert line_calls(calls)[-1] == ("leave", "999")


def test_a_lock_that_cannot_be_opened_does_not_stop_the_landing(landing, capsys):
    ln, calls = turn_landing(landing, turn=[PermissionError("denied")])
    land_in_turn(ln)
    assert call_order(calls)[-1] == "merge"
    assert "(denied); going on without a turn" in capsys.readouterr().out


def test_a_turn_whose_ssh_session_ended_is_taken_again_before_the_merge(
    landing, capsys
):
    ended = subprocess.Popen(["true"], text=True)
    ended.wait()
    ln, calls = turn_landing(landing, polls=(OPEN, OPEN, MERGED), turn=[ended, True])
    land_in_turn(ln)
    turns = ("try_turn", "release_turn", "merge")
    assert [name for name in call_order(calls) if name in turns] == [
        "try_turn",
        "release_turn",
        "try_turn",
        "merge",
        "release_turn",
    ]
    out = capsys.readouterr().out
    assert (
        "gave the landing turn back because the ssh session that held it ended" in out
    )


def test_the_turn_is_given_up_when_the_landing_ends_without_merging(landing):
    ln, calls = turn_landing(landing, await_ci=[(CI_RED, "CI red")])
    with pytest.raises(Outcome):
        land_in_turn(ln)
    assert call_order(calls)[-1] == "release_turn"


def test_a_draft_that_cannot_be_marked_ready_ends_the_landing_with_the_remedy(
    landing,
):
    ln, calls = turn_landing(landing, gh_merge_rc=[1])
    with pytest.raises(Outcome) as exc:
        land_in_turn(ln)
    assert exc.value.rc == 1
    assert "could not be marked ready (boom); run `gh pr ready 999`" in exc.value.error
    assert call_order(calls)[-1] == "release_turn"


def test_a_draft_a_secondary_limit_refuses_says_to_wait_not_to_repeat_the_call(
    landing,
):
    """GitHub names the GraphQL quota, which still has calls left, so a burst limit refused it."""
    refused = "GraphQL: API rate limit already exceeded for user ID 41801780."
    ln, _calls = turn_landing(landing, gh_merge_rc=[1], gh_stderr=refused)
    with pytest.raises(Outcome) as exc:
        land_in_turn(ln)
    assert "GitHub's secondary rate limit refused it" in exc.value.error
    assert exc.value.error.endswith("wait a few minutes, then re-run this")
    assert "gh pr ready" not in exc.value.error


def test_a_whole_landing_takes_the_turn_with_nothing_set(land_run):
    fakes = Fakes(
        gh_views={
            "state,title,body,reviewDecision": {
                "state": "OPEN",
                "title": "Take turns",
                "body": "",
            },
            "headRefOid": {"headRefOid": HEAD},
            "state,mergeable,headRefOid": [OPEN, MERGED],
            START_VIEW: {"isDraft": True, **OPEN},
        },
    )
    rc, out, _, calls, _ = land_run(["--arm-merge", "--await-merge"], fakes)
    assert rc == 0, out
    assert call_order(calls)[:4] == ["try_turn", "pr ready", "merge", "release_turn"]


def test_in_turn_a_head_with_no_ci_run_is_updated_from_master_before_its_run(landing):
    updated = {**OPEN, "headRefOid": UPDATED}
    ln, calls = turn_landing(
        landing,
        polls=(OPEN, updated, updated, {**MERGED, "headRefOid": UPDATED}),
        compare=MASTER_MOVED,
    )
    land_in_turn(ln)
    # Ready only once GitHub shows the updated head, so the one run starts there.
    assert call_order(calls) == [
        "try_turn",
        "update",
        "pr ready",
        "merge",
        "release_turn",
    ]
    assert merged_sha(calls) == UPDATED


@pytest.mark.parametrize(
    "case",
    [
        pytest.param({"start": {**OPEN, "mergeable": "CONFLICTING"}}, id="a-conflict"),
        pytest.param({"approved_head": HEAD}, id="an-approved-head"),
        pytest.param(
            {"turn": [False], "merge_timeout": 2, "merge_poll": 1}, id="no-turn"
        ),
    ],
)
def test_the_branch_is_left_alone_and_the_head_merges_as_it_would_without_a_turn(
    landing, case
):
    approved = case.pop("approved_head", "")
    ln, calls = turn_landing(landing, compare=MASTER_MOVED, **case)
    ln.approved_head = approved
    land_in_turn(ln)
    assert "update" not in call_order(calls)
    assert merged_sha(calls) == HEAD


def test_a_head_that_moved_after_the_policy_pinned_it_is_refused_and_not_updated(
    landing,
):
    ln, calls = turn_landing(landing, compare=MASTER_MOVED)
    ln.pinned_head = "d" * 40
    with pytest.raises(Outcome) as exc:
        land_in_turn(ln)
    assert "after the landing policy checked it" in exc.value.error
    assert "update" not in call_order(calls)


def test_an_update_github_refuses_leaves_the_pr_the_run_it_has(landing, capsys):
    # gh answers: the refused update, then `pr ready`, then the merge.
    ln, calls = turn_landing(landing, compare=MASTER_MOVED, gh_merge_rc=[1, 0, 0])
    land_in_turn(ln)
    assert call_order(calls)[-4:] == ["update", "pr ready", "merge", "release_turn"]
    assert merged_sha(calls) == HEAD
    assert "going on with the run the PR has" in capsys.readouterr().out


def test_an_update_github_refuses_as_a_conflict_ends_merge_conflict(landing):
    ln, calls = turn_landing(
        landing,
        compare=MASTER_MOVED,
        gh_merge_rc=[1],
        gh_stderr=f"gh: {stale_base.CONFLICT_REFUSAL} (HTTP 422)",
    )
    with pytest.raises(Outcome) as ended:
        land_in_turn(ln)
    assert ended.value.verdict == Verdict.MERGE_CONFLICT
    # No run starts on a head that cannot merge: the draft is never marked ready.
    assert call_order(calls)[-2:] == ["update", "release_turn"]


@pytest.mark.parametrize(
    "head_runs",
    [A_RUN, [{"id": 2, "status": "completed", "conclusion": "cancelled"}]],
    ids=["in-progress", "cancelled-with-no-draft-run"],
)
def test_a_head_with_a_run_takes_the_turn_only_once_it_is_green(landing, head_runs):
    """A cancelled run with no draft run beside it gets no new run from the turn, so a
    landing that took the turn for it would hold it until `merge_timeout`."""
    ln, calls = turn_landing(
        landing,
        polls=(OPEN, OPEN, MERGED),
        head_runs=head_runs,
        await_ci=[(75, "pending"), (0, "CI green")],
    )
    land_in_turn(ln)
    names = [name for name, _, _ in calls if name in ("await_ci", "try_turn")]
    # The pending read and the green read both come before the one ask for the turn.
    assert names.count("try_turn") == 1
    assert names.index("try_turn") >= 2


def test_a_green_head_that_stood_in_line_is_read_again_and_not_merged_twice(
    landing, capsys
):
    ln, calls = turn_landing(
        landing, polls=(OPEN, MERGED), head_runs=A_RUN, turn=[False, True]
    )
    land_in_turn(ln)
    # Another merge landed the PR during the wait, so this landing makes none.
    assert call_order(calls) == ["pr ready", "try_turn", "try_turn", "release_turn"]
    assert (
        "merged after 30s, 30s of it waiting for the landing turn"
        in capsys.readouterr().out
    )


def test_a_wait_in_line_at_a_green_head_counts_inside_two_merge_budgets(landing):
    ln, _ = turn_landing(
        landing,
        polls=(OPEN,),
        head_runs=A_RUN,
        turn=[False, False, True],
        await_ci=[(0, "CI green"), (75, "pending")],
        merge_timeout=3,
        merge_poll=2,
    )
    with pytest.raises(Outcome) as exc:
        land_in_turn(ln)
    # 4s in line at the green head. A whole budget from there would end at 7s, and twice
    # `merge_timeout` ends it at 6s.
    assert "still OPEN after 6s" in exc.value.error


def test_a_pr_merged_while_the_landing_waited_for_its_turn_is_not_touched(landing):
    ln, calls = turn_landing(
        landing,
        polls=(MERGED,),
        start=(OPEN, MERGED),
        turn=[False, True],
        compare=MASTER_MOVED,
    )
    land_in_turn(ln)
    assert call_order(calls) == ["try_turn", "try_turn", "release_turn"]


def test_a_landing_that_merges_nothing_itself_takes_no_turn(landing):
    ln, calls = landing(Fakes(gh_views={"state,mergeable,headRefOid": [OPEN, MERGED]}))
    ln.direct_merge_subject = ""
    land_in_turn(ln)
    assert call_order(calls) == []


def test_a_landing_off_the_turn_host_takes_the_turn_over_ssh(
    landing, tmp_path, monkeypatch, capsys
):
    seen = tmp_path / "ssh-args"
    stubs = fake_bin(
        tmp_path / "bin",
        ssh=f"#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" > {seen}\necho held\ncat >/dev/null\n",
    )
    monkeypatch.setenv("PATH", path_with(stubs))
    monkeypatch.delenv(turn.TURN_DIR_ENV)
    ln, _ = turn_landing(landing)
    ln.tools = replace(ln.tools, try_turn=None, hostname=lambda: "daniel-server")
    land_in_turn(ln)
    assert turn.TURN_HOST in seen.read_text().splitlines()
    assert "took the landing turn after 0s" in capsys.readouterr().out
