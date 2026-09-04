"""--arm-merge and --await-merge, driven directly against a fake gh.

The failure the await half guards is a landing that SITS: an armed auto-merge never fires
from CONFLICTING or from a red PR CI, and GitHub reports neither in `state`, so the loop
burned the 2700s budget and printed merge-timeout. The accept halves matter as much: GitHub
serves `mergeable: UNKNOWN` until it computes mergeability, and await_ci answers `pending`
until a required check registers.

Run: uv run pytest scripts/deploy_tools/tests/test_land_merge.py
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from _land_fakes import Fakes
from deploy_tools.land_lib import merge
from deploy_tools.land_lib.outcome import Outcome

_OPEN = {"state": "OPEN", "title": "Bump vale to 3.19.0"}


def _wait(states: list[str]):
    return [
        {"state": s, "mergeable": m, "headRefOid": h}
        for s, m, h in (x.split() for x in states)
    ]


def test_arm_merge_calls_gh_pr_merge_with_the_pr_title(landing, capsys):
    ln, calls = landing(Fakes(gh_views={"state,title": _OPEN}), arm_merge=True)
    merge.arm_merge(ln)
    assert next(c for c in calls if c[0] == "gh")[1] == (
        "pr",
        "merge",
        "999",
        "--squash",
        "--auto",
        "--subject",
        "Bump vale to 3.19.0",
    )
    assert "auto-merge armed: Bump vale to 3.19.0" in capsys.readouterr().out


def test_arm_merge_subject_overrides_the_pr_title(landing):
    ln, calls = landing(Fakes(gh_views={"state,title": _OPEN}), subject="Pin vale")
    merge.arm_merge(ln)
    assert next(c for c in calls if c[0] == "gh")[1][-1] == "Pin vale"


def test_arm_merge_is_a_no_op_on_a_merged_pr(landing, capsys):
    ln, calls = landing(Fakes(gh_views={"state,title": {**_OPEN, "state": "MERGED"}}))
    merge.arm_merge(ln)
    assert not [c for c in calls if c[0] == "gh"]
    assert "already merged" in capsys.readouterr().out


def test_arm_merge_dies_on_a_closed_pr(landing):
    ln, _ = landing(Fakes(gh_views={"state,title": {**_OPEN, "state": "CLOSED"}}))
    with pytest.raises(Outcome) as exc:
        merge.arm_merge(ln)
    assert exc.value.rc == 1 and "closed without merging" in exc.value.error


@pytest.mark.parametrize(
    "states, verdict",
    [
        (["OPEN CONFLICTING dead", "OPEN CONFLICTING dead"], "merge-conflict"),
        (
            ["OPEN CONFLICTING dead", "OPEN MERGEABLE dead", "CLOSED MERGEABLE dead"],
            None,
        ),
        (["OPEN UNKNOWN dead", "OPEN UNKNOWN dead", "CLOSED UNKNOWN dead"], None),
    ],
)
def test_only_a_settled_conflict_ends_the_wait(landing, states, verdict):
    ln, _ = landing(Fakes(gh_views={"state,mergeable,headRefOid": _wait(states)}))
    with pytest.raises(Outcome) as exc:
        merge.await_merge(ln)
    assert exc.value.rc == 1
    assert exc.value.verdict == verdict
    if verdict is None:
        assert "closed without merging" in exc.value.error


def test_a_red_pr_ci_ends_the_wait(landing):
    ln, _ = landing(
        Fakes(
            gh_views={"state,mergeable,headRefOid": _wait(["OPEN MERGEABLE dead"])},
            await_ci=[(1, "dead1234: CI RED")],
        )
    )
    with pytest.raises(Outcome) as exc:
        merge.await_merge(ln)
    assert exc.value.verdict == "pr-ci-red" and "dead1234: CI RED" in exc.value.error


@pytest.mark.parametrize("ci_rc", [75, 2])
def test_a_ci_answer_that_is_not_red_keeps_the_wait_going(landing, ci_rc):
    """75 is `pending`, the grace period; 2 is the disarmed gate, which checked nothing."""
    ln, _ = landing(
        Fakes(
            gh_views={
                "state,mergeable,headRefOid": _wait(
                    ["OPEN MERGEABLE dead", "CLOSED MERGEABLE dead"]
                )
            },
            await_ci=[(ci_rc, "pending")],
        )
    )
    with pytest.raises(Outcome) as exc:
        merge.await_merge(ln)
    assert exc.value.verdict is None and "closed without merging" in exc.value.error


def test_the_merge_budget_ends_the_wait_with_its_own_verdict(landing):
    """Unreachable in the bash harness: LAND_MERGE_POLL=0 never advanced `waited`."""
    ln, _ = landing(
        Fakes(
            gh_views={"state,mergeable,headRefOid": _wait(["OPEN MERGEABLE dead"])},
            await_ci=[(75, "pending")],
        ),
        merge_timeout=1,
        merge_poll=1,
    )
    with pytest.raises(Outcome) as exc:
        merge.await_merge(ln)
    assert (exc.value.rc, exc.value.verdict) == (75, "merge-timeout")


def test_a_merged_pr_leaves_the_wait(landing, capsys):
    ln, _ = landing(
        Fakes(gh_views={"state,mergeable,headRefOid": _wait(["MERGED MERGEABLE dead"])})
    )
    merge.await_merge(ln)
    assert "merged after 0s" in capsys.readouterr().out


@pytest.mark.parametrize(
    "state, mss, expected",
    [
        ("MERGED", "CLEAN", "already-merged"),
        ("OPEN", "CLEAN", "merge-direct"),
        ("OPEN", "BLOCKED", "die"),
        ("OPEN", "DIRTY", "die"),
        ("OPEN", "", "die"),
    ],
)
def test_arm_merge_fallback_decision(state, mss, expected):
    assert merge.arm_merge_fallback_decision(state, mss) == expected


def test_a_clean_pr_falls_through_to_a_direct_merge(landing, capsys):
    """Issue #1008: --auto rejects a CLEAN PR; the fallback merges it directly."""
    ln, calls = landing(
        Fakes(
            gh_views={
                "state,title": _OPEN,
                "state,mergeStateStatus": {
                    "state": "OPEN",
                    "mergeStateStatus": "CLEAN",
                },
            },
            gh_merge_rc=[1, 0],
        )
    )
    merge.arm_merge(ln)
    merges = [c[1] for c in calls if c[0] == "gh"]
    assert merges[0][:5] == ("pr", "merge", "999", "--squash", "--auto")
    assert merges[1] == (
        "pr",
        "merge",
        "999",
        "--squash",
        "--subject",
        "Bump vale to 3.19.0",
    )
    out = capsys.readouterr().out
    assert "merging directly" in out
    assert "merged directly: Bump vale to 3.19.0" in out


def test_a_dirty_pr_still_dies(landing):
    ln, _ = landing(
        Fakes(
            gh_views={
                "state,title": _OPEN,
                "state,mergeStateStatus": {
                    "state": "OPEN",
                    "mergeStateStatus": "DIRTY",
                },
            },
            gh_merge_rc=[1],
        )
    )
    with pytest.raises(Outcome) as exc:
        merge.arm_merge(ln)
    assert exc.value.rc == 1 and "mergeStateStatus=DIRTY" in exc.value.error


def test_a_merge_that_lands_while_arming_reads_as_success(landing, capsys):
    ln, calls = landing(
        Fakes(
            gh_views={
                "state,title": _OPEN,
                "state,mergeStateStatus": {
                    "state": "MERGED",
                    "mergeStateStatus": "CLEAN",
                },
            },
            gh_merge_rc=[1],
        )
    )
    merge.arm_merge(ln)
    assert len([c for c in calls if c[0] == "gh"]) == 1
    assert "merged in the meantime" in capsys.readouterr().out


def test_an_auto_exit_0_with_no_auto_merge_request_merges_directly(landing, capsys):
    """Issue #1029: --auto exited 0 on PR #1026 and autoMergeRequest stayed null."""
    ln, calls = landing(
        Fakes(
            gh_views={
                "state,title": _OPEN,
                "state,mergeStateStatus,autoMergeRequest": {
                    "state": "OPEN",
                    "mergeStateStatus": "CLEAN",
                    "autoMergeRequest": None,
                },
            }
        )
    )
    merge.arm_merge(ln)
    merges = [c[1] for c in calls if c[0] == "gh"]
    assert len(merges) == 2 and "--auto" not in merges[1]
    out = capsys.readouterr().out
    assert "not armed" in out and "merged directly: Bump vale to 3.19.0" in out


def test_an_unarmed_pr_that_is_not_clean_dies_rather_than_merging(landing):
    """The reject half of #1029: direct-merging a BLOCKED PR would fail the same way."""
    ln, calls = landing(
        Fakes(
            gh_views={
                "state,title": _OPEN,
                "state,mergeStateStatus,autoMergeRequest": {
                    "state": "OPEN",
                    "mergeStateStatus": "BLOCKED",
                    "autoMergeRequest": None,
                },
            }
        )
    )
    with pytest.raises(Outcome) as exc:
        merge.arm_merge(ln)
    assert exc.value.rc == 1
    assert "not armed (mergeStateStatus=BLOCKED)" in exc.value.error
    assert len([c for c in calls if c[0] == "gh"]) == 1


def test_a_pr_that_merged_during_the_arm_is_not_merged_again(landing, capsys):
    """The read-back finding MERGED is the same race the rejection path already handles."""
    ln, calls = landing(
        Fakes(
            gh_views={
                "state,title": _OPEN,
                "state,mergeStateStatus,autoMergeRequest": {
                    "state": "MERGED",
                    "mergeStateStatus": "CLEAN",
                    "autoMergeRequest": None,
                },
            }
        )
    )
    merge.arm_merge(ln)
    assert len([c for c in calls if c[0] == "gh"]) == 1
    assert "PR #999 merged in the meantime" in capsys.readouterr().out


def test_a_read_back_that_fails_trusts_the_exit_code(landing, capsys):
    """A read-back is a confirmation, not a gate: gh failing here must not fail a landing
    whose arm may well have worked, and must not double-merge on a guess."""
    ln, calls = landing(Fakes(gh_views={"state,title": _OPEN}))
    real = ln.tools.gh_json

    def gh_json(*args, **kwargs):
        if "state,mergeStateStatus,autoMergeRequest" in args:
            raise subprocess.CalledProcessError(1, "gh", stderr="HTTP 502")
        return real(*args, **kwargs)

    ln.tools.gh_json = gh_json
    merge.arm_merge(ln)
    assert len([c for c in calls if c[0] == "gh"]) == 1
    assert "trusting gh pr merge --auto's exit 0" in capsys.readouterr().out


def test_a_verified_arm_says_armed_and_merges_nothing_directly(landing, capsys):
    ln, calls = landing(
        Fakes(
            gh_views={
                "state,title": _OPEN,
                "state,mergeStateStatus,autoMergeRequest": {
                    "state": "OPEN",
                    "mergeStateStatus": "BLOCKED",
                    "autoMergeRequest": {"enabledAt": "x"},
                },
            }
        )
    )
    merge.arm_merge(ln)
    assert len([c for c in calls if c[0] == "gh"]) == 1
    assert "auto-merge armed" in capsys.readouterr().out


def test_the_merge_wait_never_hand_polls_ci():
    """The nudge-land-sh hook denies `gh pr checks --watch` and `gh run watch`, and merge.py
    is where someone would "improve" the wait by adding one. await_ci owns the CI verdict:
    it is one shot per poll, derived from the required checks, and its `pending` IS the
    grace period. A textual guard because the failure is a command that never appears in
    any test's call log until it is already in production."""
    text = Path(merge.__file__).read_text()
    assert "gh pr checks" not in text
    assert '"run"' not in text
    assert "await_ci" in text
