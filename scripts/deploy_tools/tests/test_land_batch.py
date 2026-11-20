"""A landing that takes the turn with a draft tests the drafts in the line with its own.

Run: uv run pytest scripts/deploy_tools/tests/test_land_batch.py

`_land_batch_fakes.GitHub` models the repository, and the tests read it afterwards. They
assert what happened to each PR, not which calls ran.
"""

import os
import subprocess
from dataclasses import replace

import pytest

from dev import tested_tree
from _land_batch_fakes import GREEN, OWN, answers, head_of, land
from _land_fakes import Fakes
from deploy_tools.land_lib import batch, gh_read, line, live_limit, turn
from lib.exit_codes import CI_PENDING, CI_RED


def test_the_drafts_in_the_line_are_tested_in_one_run_and_merged_in_order(
    batch_of, capsys
):
    ln, hub, the_line = batch_of(drafts=("7", "9"))
    land(ln)

    # One branch, stacked on master: the holder's head, then the line's, in line order.
    assert hub.stacked == {f"queue/{OWN}": [head_of(OWN), head_of("7"), head_of("9")]}
    [opened] = hub.batch_prs
    assert (opened["head"], opened["base"]) == (f"queue/{OWN}", "master")
    # The batch's gate reads the members from the body the landing wrote.
    assert tested_tree.batch_members(opened["body"]) == [999, 7, 9]
    assert hub.merged == [OWN, "7", "9"]
    # Each PR carried the label when its own run started, and the branch is gone.
    assert hub.labels_at_ready == {pr: {batch.LABEL} for pr in (OWN, "7", "9")}
    assert hub.branches == {}
    # Each member's own landing reads that its PR merged.
    assert answers(the_line, "7", "9") == [line.MERGED, line.MERGED]
    # Nobody needed a second run.
    assert hub.full_runs == []


def test_a_head_the_batch_tested_gets_no_branch_update_and_no_stale_base_check(
    batch_of,
):
    # Master moved on a path every PR here changes. Without a batch, the turn updates the
    # holder's branch before its run, and the merge wait updates a member's as stale.
    moved = Fakes(
        compare=[["docs/notes.md"]],
        pr_files=[{"filename": "docs/notes.md", "status": "modified"}],
    )
    ln, hub, _ = batch_of(drafts=("7",), fakes=moved)
    land(ln)
    assert hub.merged == [OWN, "7"]
    updates = [
        args
        for name, args, _ in hub.calls
        if name == "gh" and args[3].endswith("/update-branch")
    ]
    assert updates == []


def test_another_landing_of_the_holders_own_pr_is_not_stacked_behind_it(batch_of):
    ln, hub, the_line = batch_of(drafts=("7",))
    the_line("join", OWN, head_of(OWN))
    land(ln)
    assert hub.stacked[f"queue/{OWN}"] == [head_of(OWN), head_of("7")]
    assert hub.merged == [OWN, "7"]


def test_a_batch_holds_as_many_prs_as_its_gate_reads():
    assert batch.MAX_STACKED == tested_tree.MAX_MEMBERS


def test_a_line_longer_than_a_batch_leaves_the_rest_waiting(batch_of):
    ln, hub, the_line = batch_of(drafts=("3", "5", "7", "9"))
    land(ln)
    assert hub.merged == [OWN, "3", "5", "7"]
    assert answers(the_line, "9") == [""]
    assert hub.labels["9"] == set()


def test_a_landing_with_nobody_in_the_line_lands_its_draft_alone(batch_of):
    ln, hub, _ = batch_of()
    land(ln)
    assert hub.merged == [OWN]
    assert hub.stacked == {} and hub.batch_prs == []
    assert hub.labels_at_ready == {OWN: set()}


@pytest.mark.parametrize(
    ("fakes", "why"),
    [
        (Fakes(deploy_hold="abc123"), "holds 'abc123', and this PR changes a k8s role"),
        (
            Fakes(in_window=True),
            "k8s role inside the weekly reboot's no-promotion window",
        ),
    ],
    ids=["under-a-hold", "inside-the-window"],
)
def test_a_draft_a_rule_bars_is_left_out_of_the_batch(batch_of, capsys, fakes, why):
    k8s = {"files": [{"path": "k8s/roles/sonarr/defaults/main.yml"}]}
    ln, hub, _ = batch_of(drafts=("7", "9"), over={"7": k8s}, fakes=fakes)
    land(ln)
    assert hub.merged == [OWN, "9"]
    out = capsys.readouterr().out
    assert "leaving PR #7 out of the batch: " in out and why in out


@pytest.mark.parametrize(
    ("over", "github", "why"),
    [
        pytest.param(
            {"files": [{"path": "k8s/roles/flux/defaults/main.yml"}]},
            {},
            f"it changes {live_limit.SERIAL_WHAT}",
            id="a-serial-pr",
        ),
        pytest.param(
            {"isCrossRepository": True},
            {},
            "it is not a branch of this repository into master",
            id="a-fork",
        ),
        pytest.param(
            {"baseRefName": "release"},
            {},
            "it is not a branch of this repository into master",
            id="another-base",
        ),
        pytest.param(
            {"headRefOid": "f" * 40},
            {},
            "its head moved after its landing stood in the line",
            id="a-moved-head",
        ),
        pytest.param(
            {"isDraft": False}, {}, "it is no longer an open draft", id="a-ready-pr"
        ),
        pytest.param(
            {"mergeable": "CONFLICTING"},
            {},
            "it conflicts with master",
            id="a-conflict",
        ),
        pytest.param(
            {"reviewDecision": "CHANGES_REQUESTED"},
            {},
            "PR #7 has changes requested",
            id="what-its-own-landing-refuses",
        ),
        pytest.param(
            {},
            {"conflicts": [head_of("7")]},
            "it does not merge onto the PRs stacked before it",
            id="a-conflict-with-the-stack",
        ),
    ],
)
def test_a_draft_the_landing_may_not_stack_is_left_in_the_line(
    batch_of, capsys, over, github, why
):
    ln, hub, the_line = batch_of(drafts=("7", "9"), over={"7": over}, **github)
    land(ln)
    assert hub.merged == [OWN, "9"]
    assert head_of("7") not in hub.stacked[f"queue/{OWN}"]
    assert hub.labels["7"] == set()
    assert f"leaving PR #7 out of the batch: {why}" in capsys.readouterr().out
    assert answers(the_line, "9") == [line.MERGED]


@pytest.mark.parametrize(
    "opts",
    [
        pytest.param({"batch": False}, id="land-batch-0"),
        # Two budgets of 599 s hold no member's twice ten minutes.
        pytest.param(
            {"merge_timeout": batch.MEMBER_TIMEOUT_S - 1}, id="no-room-for-a-member"
        ),
    ],
)
def test_a_landing_that_could_merge_no_member_forms_no_batch(batch_of, opts):
    ln, hub, the_line = batch_of(drafts=("7",), opts=opts)
    land(ln)
    assert hub.merged == [OWN]
    assert hub.stacked == {}
    assert answers(the_line, "7") == [""]


def test_a_holder_that_must_land_alone_forms_no_batch(batch_of):
    serial = {"files": [{"path": "secrets/cluster-vars.sops.yaml"}]}
    ln, hub, the_line = batch_of(drafts=("7",), over={OWN: serial})
    land(ln)
    assert hub.merged == [OWN]
    assert hub.stacked == {}
    assert answers(the_line, "7") == [""]


def test_a_holder_whose_own_head_does_not_merge_forms_no_batch(batch_of):
    ln, hub, _ = batch_of(drafts=("7",), conflicts=[head_of(OWN)])
    land(ln)
    # The branch is dropped and no PR is opened; the landing goes on with its own PR.
    assert hub.branches == {} and hub.batch_prs == []
    assert hub.merged == [OWN]


@pytest.mark.parametrize(
    ("run", "opts", "said"),
    [
        pytest.param([(CI_RED, "CI RED")], {}, "its run is red (CI RED)", id="red"),
        pytest.param(
            [(CI_PENDING, "pending")],
            {"merge_poll": batch.RUN_TIMEOUT_S},
            f"its run gave no verdict in {batch.RUN_TIMEOUT_S}s (pending)",
            id="no-verdict",
        ),
    ],
)
def test_a_batch_that_is_not_green_leaves_every_pr_to_land_alone(
    batch_of, capsys, run, opts, said
):
    ln, hub, the_line = batch_of(drafts=("7", "9"), run=run, opts=opts)
    land(ln)
    # The holder's own PR gets a run of its own, with no label, and the members wait on.
    assert hub.merged == [OWN]
    assert hub.labels_at_ready == {OWN: set()}
    assert hub.labels == {OWN: set(), "7": set(), "9": set()}
    assert hub.branches == {}
    # Each member is told, so the next landing to take the turn does not stack it again.
    assert answers(the_line, "7", "9") == [line.ALONE, line.ALONE]
    assert f"the batch is not green: {said}" in capsys.readouterr().out


@pytest.mark.parametrize(
    "turn",
    [
        pytest.param([False, False, True], id="read-on-a-poll"),
        # The holder answered and gave the turn up inside one poll of this landing.
        pytest.param([False, True], id="read-on-taking-the-turn"),
    ],
)
def test_a_landing_told_to_land_alone_forms_no_batch_of_its_own(batch_of, turn):
    ln, hub, the_line = batch_of(drafts=("7",), fakes=Fakes(turn=turn))
    nap = ln.tools.sleep
    answers = [line.ALONE]

    def sleep(seconds: float) -> None:
        # The landing that holds the turn answers once, during this landing's first wait.
        if answers:
            the_line("answer", OWN, head_of(OWN), answers.pop())
        nap(seconds)

    ln.tools = replace(ln.tools, sleep=sleep)
    land(ln)
    assert hub.stacked == {} and hub.batch_prs == []
    # Its own PR gets a run of its own, with no label, and the other draft waits on.
    assert hub.merged == [OWN] and hub.labels_at_ready == {OWN: set()}
    assert the_line("waiting", str(line.FRESH_S)).split() == ["7", head_of("7")]


def test_the_batchs_run_counts_as_waited(batch_of, capsys):
    ln, _, _ = batch_of(
        drafts=("7",),
        run=[(CI_PENDING, "pending")] * 2 + [GREEN],
        opts={"merge_poll": 20},
    )
    land(ln)
    assert "merged after 40s, 40s of it on its batch's run" in capsys.readouterr().out


@pytest.mark.parametrize(
    "refuse",
    ["POST git/refs", "POST merges", "POST pulls", f"POST issues/{OWN}/labels"],
)
def test_a_batch_github_refuses_partway_leaves_the_landing_its_own_pr(
    batch_of, capsys, refuse
):
    ln, hub, the_line = batch_of(drafts=("7",), refuse=refuse)
    land(ln)
    assert hub.merged == [OWN]
    assert hub.branches == {}
    assert hub.labels == {OWN: set(), "7": set()}
    assert answers(the_line, "7") == [""]
    assert "could not form a landing batch" in capsys.readouterr().out


def test_landing_code_older_than_masters_hands_the_turn_to_a_fresh_landing(
    batch_of, tmp_path
):
    # Master's landing code changed while this landing waited for the turn.
    ln, hub, the_line = batch_of(
        drafts=("7",), fakes=Fakes(stale_code=["scripts/deploy_tools/land.py"])
    )
    ln.tools = replace(ln.tools, try_turn=lambda: turn.try_take(tmp_path / "turn.lock"))
    with pytest.raises(turn.HandOver) as handed:
        land(ln)
    os.close(handed.value.fd)
    assert hub.stacked == {} and hub.batch_prs == [] and hub.merged == []
    assert the_line("waiting", str(line.FRESH_S)).split() == ["7", head_of("7")]


def test_older_landing_code_with_a_turn_it_cannot_hand_over_forms_no_batch(batch_of):
    ln, hub, the_line = batch_of(
        drafts=("7",), fakes=Fakes(stale_code=["scripts/deploy_tools/land.py"])
    )
    land(ln)
    assert hub.stacked == {} and hub.batch_prs == []
    assert hub.merged == [OWN]
    assert the_line("waiting", str(line.FRESH_S)).split() == ["7", head_of("7")]


def test_a_bug_in_the_batch_leaves_the_landing_its_own_pr(batch_of, capsys):
    ln, hub, _ = batch_of(drafts=("7",), crash="POST pulls")
    land(ln)
    assert hub.merged == [OWN]
    assert hub.branches == {}
    assert "7" not in hub.ever_labelled
    assert "the landing batch failed (RuntimeError('a bug in the batch'))" in (
        capsys.readouterr().out
    )


def test_a_holder_that_gave_the_turn_back_for_a_rate_limit_forms_no_batch(batch_of):
    ln, hub, _ = batch_of(drafts=("7",))
    # The read before the turn fails for good, so the turn's rule reads the files again,
    # and that read inside the turn is refused once more.
    refusals = iter(range(gh_read.RATE_LIMIT_WAITS + 2))
    limited = subprocess.CalledProcessError(
        1, "gh", stderr="gh: You have exceeded a secondary rate limit. (HTTP 403)"
    )

    def gh_json(*args, **kwargs):
        if "files,headRefOid" in args and next(refusals, None) is not None:
            raise limited
        return hub.gh_json(*args, **kwargs)

    ln.tools = replace(ln.tools, gh_json=gh_json)
    land(ln)
    assert hub.stacked == {} and hub.batch_prs == []
    assert hub.merged == [OWN]
