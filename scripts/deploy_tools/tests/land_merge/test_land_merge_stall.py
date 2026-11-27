"""The merge wait on a head whose CI no longer moves on its own, driven against a fake gh.

A draft-skipped head gets a draft toggle and a no-run-left head a re-run of its newest cancelled
run, once each per head. `test_land_stall_properties.py` holds the decision over generated polls;
these hold what the landing does with it. Text is asserted only where `test_land_merge.py`'s
module docstring allows it.

Run: uv run pytest scripts/deploy_tools/tests/land_merge/test_land_merge_stall.py
"""

import subprocess

import pytest

from _land_fakes import Fakes
from deploy_tools import ci_stall
from deploy_tools.land_lib import merge, stall
from deploy_tools.land_lib.outcome import Outcome
from test_land_merge import _wait


_SKIPPED = f"dead1234: no verdict after 0s — {ci_stall.DRAFT_SKIPPED}"


def _skipped_landing(landing, graces=3, is_draft=False, **fakes):
    """A landing whose head's CI reads skipped as a draft, polled thrice per grace."""
    fakes.setdefault("await_ci", [(75, _SKIPPED)])
    views = {"state,mergeable,headRefOid": _wait(["OPEN MERGEABLE dead"])}
    views.update(fakes.pop("gh_views", {}))
    return landing(
        Fakes(gh_views={**views, "isDraft": {"isDraft": is_draft}}, **fakes),
        merge_timeout=stall.GRACE_S * graces,
        merge_poll=stall.GRACE_S // 3,
    )


@pytest.mark.parametrize("is_draft, ends_early", [(False, True), (True, False)])
def test_a_head_ci_skipped_as_a_draft_ends_the_wait_once_the_pr_is_ready(
    landing, capsys, is_draft, ends_early
):
    """A ready PR whose head's only run had skipped as a draft waited out
    its whole budget. A PR still a draft gets its run once it is marked ready, so it waits.
    The ending's message is what tells the two apart, so it is asserted."""
    ln, _ = _skipped_landing(landing, is_draft=is_draft)
    with pytest.raises(Outcome) as exc:
        merge.await_merge(ln)
    assert (exc.value.rc, exc.value.verdict) == (75, "merge-timeout")
    assert ("gh pr ready --undo 999" in exc.value.error) is ends_early
    assert ("still OPEN after" in exc.value.error) is not ends_early
    assert capsys.readouterr().out.count(ci_stall.DRAFT_SKIPPED) == 1


def _ready_toggles(calls):
    return [c[1] for c in calls if c[0] == "gh" and c[1][:2] == ("pr", "ready")]


_DRAFT_RUN = {"id": 1, "status": "completed", "conclusion": "skipped"}


@pytest.mark.parametrize(
    "head_runs",
    [
        [_DRAFT_RUN],
        [_DRAFT_RUN, {"id": 2, "status": "completed", "conclusion": "cancelled"}],
    ],
    ids=["draft-run", "cancelled-ready-run"],
)
def test_a_ready_pr_whose_head_skipped_as_a_draft_is_converted_once_and_the_wait_goes_on(
    landing, capsys, head_runs
):
    """GitHub starts no run on the ready flip, so the landing converts the PR to draft
    and back itself. The second poll after the toggle reads a run, so the wait proceeds to the
    merge instead of ending merge-timeout. A ready run the draft run cancelled is past
    cancelling, so it does not stop the conversion."""
    states = ["OPEN MERGEABLE dead"] * 5 + ["MERGED MERGEABLE dead"]
    # One skipped poll per third of the grace, then the run the toggle started goes green.
    ln, calls = _skipped_landing(
        landing,
        gh_views={"state,mergeable,headRefOid": _wait(states)},
        await_ci=[(75, _SKIPPED)] * 4 + [(0, "dead1234: green")],
        head_runs=head_runs,
    )
    merge.await_merge(ln)
    assert _ready_toggles(calls) == [
        ("pr", "ready", "--undo", "999"),
        ("pr", "ready", "999"),
    ]
    assert "converting it to draft and back" in capsys.readouterr().out


def test_a_second_draft_skip_on_the_same_head_after_the_toggle_ends_merge_timeout(
    landing,
):
    """No loop: the toggle is once per head, and the dead end after it says it already ran."""
    ln, calls = _skipped_landing(landing, graces=10)
    with pytest.raises(Outcome) as exc:
        merge.await_merge(ln)
    assert (exc.value.rc, exc.value.verdict) == (75, "merge-timeout")
    assert len(_ready_toggles(calls)) == 2
    assert "already converted it to draft and back" in exc.value.error


@pytest.mark.parametrize(
    "gh_rc, left_draft", [([1], False), ([0, 1], True)], ids=["to-draft", "back"]
)
def test_a_toggle_a_secondary_limit_refuses_says_to_wait_not_to_toggle_by_hand(
    landing, gh_rc, left_draft
):
    refused = "GraphQL: API rate limit already exceeded for user ID 41801780."
    ln, _calls = _skipped_landing(landing, gh_merge_rc=gh_rc, gh_stderr=refused)
    with pytest.raises(Outcome) as exc:
        merge.await_merge(ln)
    assert (exc.value.rc, exc.value.verdict) == (75, "merge-timeout")
    assert "GitHub's secondary rate limit refused it" in exc.value.error
    assert exc.value.error.endswith("wait a few minutes, then re-run this")
    assert "gh pr ready" not in exc.value.error
    assert ("is left as a draft" in exc.value.error) is left_draft


@pytest.mark.parametrize(
    "head_runs",
    [
        [_DRAFT_RUN, {"id": 2, "status": "queued", "conclusion": None}],
        [_DRAFT_RUN, {"id": 2, "status": "in_progress", "conclusion": None}],
        [_DRAFT_RUN, {"id": 2, "status": "completed", "conclusion": "failure"}],
        [{"id": 2, "status": "completed", "conclusion": "cancelled"}],
        subprocess.CalledProcessError(1, "gh", stderr="HTTP 404: Not Found"),
        {"workflow_runs": []},
        ["not a run"],
    ],
    ids=[
        "queued",
        "in-progress",
        "failed",
        "cancelled-alone",
        "gh-fails",
        "not-a-list",
        "not-a-run",
    ],
)
def test_a_ready_run_or_an_unreadable_run_list_stops_the_conversion(landing, head_runs):
    """The ready run's gate has no check run until its jobs finish, so await_ci reads
    only the draft run's skipped gate, and converting cancelled the ready run on 6443 and 6531.
    The conversion is what cancels, so a runs read that fails or parses wrong also stops it."""
    skipped = f"dead1234: no verdict after 0s — {ci_stall.DRAFT_SKIPPED}"
    grace = stall.GRACE_S
    ln, calls = landing(
        Fakes(
            gh_views={
                "state,mergeable,headRefOid": _wait(["OPEN MERGEABLE dead"]),
                "isDraft": {"isDraft": False},
            },
            await_ci=[(75, skipped)],
            head_runs=head_runs,
        ),
        merge_timeout=grace * 3,
        merge_poll=grace // 3,
    )
    with pytest.raises(Outcome) as exc:
        merge.await_merge(ln)
    assert (exc.value.rc, exc.value.verdict) == (75, "merge-timeout")
    assert _ready_toggles(calls) == []


def test_a_failed_conversion_ends_merge_timeout_and_names_a_draft_left_behind(landing):
    skipped = f"dead1234: no verdict after 0s — {ci_stall.DRAFT_SKIPPED}"
    grace = stall.GRACE_S
    ln, _calls = landing(
        Fakes(
            gh_views={
                "state,mergeable,headRefOid": _wait(["OPEN MERGEABLE dead"]),
                "isDraft": {"isDraft": False},
            },
            await_ci=[(75, skipped)],
            gh_merge_rc=[0, 1],
        ),
        merge_timeout=grace * 10,
        merge_poll=grace // 3,
    )
    with pytest.raises(Outcome) as exc:
        merge.await_merge(ln)
    assert (exc.value.rc, exc.value.verdict) == (75, "merge-timeout")
    assert "left as a draft" in exc.value.error


_NO_RUN = f"dead1234: no verdict after 0s — {ci_stall.NO_RUN_LEFT}"
# Newest first, as GitHub lists them.
_CANCELLED_RUNS = [
    {"id": 5, "status": "completed", "conclusion": "cancelled"},
    {"id": 4, "status": "completed", "conclusion": "cancelled"},
]


def _reruns(calls):
    return [c[1] for c in calls if c[0] == "gh" and c[1][:2] == ("run", "rerun")]


def test_a_ready_head_with_no_run_left_re_runs_its_newest_cancelled_run_and_merges(
    landing,
):
    """A cancelled run is never re-run on its own, so the landing held the turn in silence
    until `merge_timeout`. It now re-runs the newest one, whose verdict the wait then merges on."""
    states = ["OPEN MERGEABLE dead"] * 5 + ["MERGED MERGEABLE dead"]
    ln, calls = _skipped_landing(
        landing,
        gh_views={"state,mergeable,headRefOid": _wait(states)},
        await_ci=[(75, _NO_RUN)] * 4 + [(0, "dead1234: green")],
        head_runs=_CANCELLED_RUNS,
    )
    merge.await_merge(ln)
    assert _reruns(calls) == [("run", "rerun", "5")]
    assert _ready_toggles(calls) == []


def test_a_head_with_no_run_left_after_its_re_run_ends_merge_timeout(landing):
    ln, calls = _skipped_landing(
        landing, graces=10, await_ci=[(75, _NO_RUN)], head_runs=_CANCELLED_RUNS
    )
    with pytest.raises(Outcome) as exc:
        merge.await_merge(ln)
    assert (exc.value.rc, exc.value.verdict) == (75, "merge-timeout")
    assert len(_reruns(calls)) == 1
    assert "already re-ran its cancelled run" in exc.value.error


def test_a_run_in_progress_stops_the_re_run(landing):
    """A re-run in the same concurrency group would cancel the run still going."""
    ln, calls = _skipped_landing(
        landing,
        await_ci=[(75, _NO_RUN)],
        head_runs=[{"id": 6, "status": "in_progress", "conclusion": None}],
    )
    with pytest.raises(Outcome) as exc:
        merge.await_merge(ln)
    assert (exc.value.rc, exc.value.verdict) == (75, "merge-timeout")
    assert _reruns(calls) == []
