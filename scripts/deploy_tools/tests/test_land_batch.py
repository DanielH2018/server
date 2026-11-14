"""A landing that takes the turn with a draft tests the drafts in the line with its own.

Run: uv run pytest scripts/deploy_tools/tests/test_land_batch.py

`_land_batch_fakes.GitHub` models the repository, and the tests read it afterwards. They
assert what happened to each PR, not which calls ran.
"""

from dataclasses import replace

import pytest

import tested_tree
from _land_batch_fakes import GREEN, OWN, GitHub, head_of
from _land_fakes import Fakes
from deploy_tools.land_lib import batch, line, turn
from deploy_tools.land_lib.outcome import Outcome
from lib.exit_codes import CI_PENDING, CI_RED


@pytest.fixture
def batch_of(landing, tmp_path):
    """A draft's landing that holds the turn, with `drafts` standing in the line behind it.

    Returns the landing, the repository model and the line. `over` changes a PR as the
    repository shows it, by number.
    """

    def build(drafts=(), over=None, run=(GREEN,), opts=None, fakes=None, **github):
        flags = {"queue": True, "arm_merge": True, "await_merge": True, "batch": True}
        ln, calls = landing(fakes or Fakes(), **{**flags, **(opts or {})})
        the_line = line.here(tmp_path / "line")
        prs = {pr: {} for pr in (OWN, *drafts)}
        for pr, changes in (over or {}).items():
            prs[pr] = changes
        hub = GitHub(ln.tools, prs, run, **github)
        hub.calls = calls
        ln.tools = replace(
            ln.tools,
            gh_json=hub.gh_json,
            gh=hub.gh,
            await_ci=hub.await_ci,
            line=the_line,
        )
        ln.direct_merge_subject = "Own"
        for pr in drafts:
            the_line("join", pr, head_of(pr))
        return ln, hub, the_line

    return build


def _land(ln) -> None:
    """The merge wait as the pipeline runs it."""
    with turn.held(ln) as take_turn:
        batch.land(ln, take_turn)


def _answers(the_line, *prs) -> list[str]:
    """What each PR's own landing reads in its place on its next poll."""
    return [the_line("stand", pr, head_of(pr)) for pr in prs]


def test_the_drafts_in_the_line_are_tested_in_one_run_and_merged_in_order(
    batch_of, capsys
):
    ln, hub, the_line = batch_of(drafts=("7", "9"))
    _land(ln)

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
    assert _answers(the_line, "7", "9") == [line.MERGED, line.MERGED]
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
    _land(ln)
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
    _land(ln)
    assert hub.stacked[f"queue/{OWN}"] == [head_of(OWN), head_of("7")]
    assert hub.merged == [OWN, "7"]


def test_a_batch_holds_as_many_prs_as_its_gate_reads():
    assert batch.MAX_STACKED == tested_tree.MAX_MEMBERS


def test_a_line_longer_than_a_batch_leaves_the_rest_waiting(batch_of):
    ln, hub, the_line = batch_of(drafts=("3", "5", "7", "9"))
    _land(ln)
    assert hub.merged == [OWN, "3", "5", "7"]
    assert _answers(the_line, "9") == [""]
    assert hub.labels["9"] == set()


def test_a_landing_with_nobody_in_the_line_lands_its_draft_alone(batch_of):
    ln, hub, _ = batch_of()
    _land(ln)
    assert hub.merged == [OWN]
    assert hub.stacked == {} and hub.batch_prs == []
    assert hub.labels_at_ready == {OWN: set()}


@pytest.mark.parametrize(
    ("over", "github", "why"),
    [
        pytest.param(
            {"files": [{"path": "k8s/roles/flux/defaults/main.yml"}]},
            {},
            "it changes a forward-only pin, the flux role or cluster-vars",
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
    _land(ln)
    assert hub.merged == [OWN, "9"]
    assert head_of("7") not in hub.stacked[f"queue/{OWN}"]
    assert hub.labels["7"] == set()
    assert f"leaving PR #7 out of the batch: {why}" in capsys.readouterr().out
    assert _answers(the_line, "9") == [line.MERGED]


def test_a_landing_forms_no_batch_unless_it_is_asked_to(batch_of):
    ln, hub, the_line = batch_of(drafts=("7",), opts={"batch": False})
    _land(ln)
    assert hub.merged == [OWN]
    assert hub.stacked == {}
    assert _answers(the_line, "7") == [""]


def test_a_holder_that_must_land_alone_forms_no_batch(batch_of):
    serial = {"files": [{"path": "secrets/cluster-vars.sops.yaml"}]}
    ln, hub, the_line = batch_of(drafts=("7",), over={OWN: serial})
    _land(ln)
    assert hub.merged == [OWN]
    assert hub.stacked == {}
    assert _answers(the_line, "7") == [""]


def test_a_holder_whose_own_head_does_not_merge_forms_no_batch(batch_of):
    ln, hub, _ = batch_of(drafts=("7",), conflicts=[head_of(OWN)])
    _land(ln)
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
    _land(ln)
    # The holder's own PR gets a run of its own, with no label, and the members wait on.
    assert hub.merged == [OWN]
    assert hub.labels_at_ready == {OWN: set()}
    assert hub.labels == {OWN: set(), "7": set(), "9": set()}
    assert hub.branches == {}
    assert _answers(the_line, "7", "9") == ["", ""]
    assert f"the batch is not green: {said}" in capsys.readouterr().out


def test_the_batchs_run_counts_as_waited(batch_of, capsys):
    ln, _, _ = batch_of(
        drafts=("7",),
        run=[(CI_PENDING, "pending")] * 2 + [GREEN],
        opts={"merge_poll": 20},
    )
    _land(ln)
    assert "merged after 40s, 40s of it on its batch's run" in capsys.readouterr().out


@pytest.mark.parametrize(
    "refuse",
    ["POST git/refs", "POST merges", "POST pulls", f"POST issues/{OWN}/labels"],
)
def test_a_batch_github_refuses_partway_leaves_the_landing_its_own_pr(
    batch_of, capsys, refuse
):
    ln, hub, the_line = batch_of(drafts=("7",), refuse=refuse)
    _land(ln)
    assert hub.merged == [OWN]
    assert hub.branches == {}
    assert hub.labels == {OWN: set(), "7": set()}
    assert _answers(the_line, "7") == [""]
    assert "could not form a landing batch" in capsys.readouterr().out


def test_a_member_whose_landing_left_the_line_ends_the_batch_there(batch_of, capsys):
    ln, hub, the_line = batch_of(drafts=("5", "7", "9"))
    real = ln.tools.line

    def leaves_during_the_run(op, *args):
        # PR 7's landing gives up while the holder merges the PRs before it.
        if op == "answer" and args[0] == "5":
            the_line("leave", "7")
        return real(op, *args)

    ln.tools = replace(ln.tools, line=leaves_during_the_run)
    _land(ln)
    assert hub.merged == [OWN, "5"]
    # Neither it nor the PR stacked on it was ever labelled, so each run alone is a full one.
    assert hub.ever_labelled == {OWN, "5"}
    assert _answers(the_line, "9") == [""]
    out = capsys.readouterr().out
    assert "PR #7 is not merged (its own landing left the line)" in out


def test_master_moving_under_the_batch_ends_it_before_the_next_member(batch_of, capsys):
    ln, hub, the_line = batch_of(drafts=("7", "9"))
    real = ln.tools.gh

    def another_landing_merges_first(*args, **kwargs):
        done = real(*args, **kwargs)
        if hub.merged == [OWN, "7"]:
            hub.master = "e" * 40
        return done

    ln.tools = replace(ln.tools, gh=another_landing_merges_first)
    _land(ln)
    assert hub.merged == [OWN, "7"]
    assert "9" not in hub.ever_labelled
    assert _answers(the_line, "7", "9") == [line.MERGED, ""]
    out = capsys.readouterr().out
    assert "the batch's run no longer stands for PR #9: master is eeeeeeee" in out
    assert "PR #9 is not merged" in out


def test_master_moving_during_the_batchs_run_leaves_the_holder_to_the_stale_rules(
    batch_of, capsys
):
    ln, hub, the_line = batch_of(drafts=("7",))
    real = ln.tools.await_ci

    def master_moves_during_the_run(sha, timeout):
        if sha == hub.branches.get(f"queue/{OWN}"):
            hub.master = "e" * 40
        return real(sha, timeout)

    ln.tools = replace(ln.tools, await_ci=master_moves_during_the_run)
    _land(ln)
    # The holder merges as any green head does, and the member is not merged behind it.
    assert hub.merged == [OWN]
    assert "7" not in hub.ever_labelled
    assert _answers(the_line, "7") == [""]
    out = capsys.readouterr().out
    assert "the stale-base rules judge it" in out
    assert "the batch's run no longer stands, so its other PRs land alone" in out


def _during_the_run(ln, hub, change) -> None:
    """Run `change` when the landing first reads the batch's run."""
    real = ln.tools.await_ci
    done = []

    def await_ci(sha, timeout):
        if sha == hub.branches.get(f"queue/{OWN}") and not done:
            done.append(change())
        return real(sha, timeout)

    ln.tools = replace(ln.tools, await_ci=await_ci)


def test_a_member_pushed_during_the_batchs_run_is_not_merged(batch_of, capsys):
    ln, hub, the_line = batch_of(drafts=("7", "9"))
    _during_the_run(ln, hub, lambda: hub.prs["7"].update(headRefOid="f" * 40))
    _land(ln)
    # The batch tested its old head, so neither it nor the PR stacked on it is labelled.
    assert hub.merged == [OWN]
    assert hub.ever_labelled == {OWN}
    assert _answers(the_line, "7", "9") == ["", ""]
    assert "its head is ffffffff, not the 00000000 the batch stacked" in (
        capsys.readouterr().out
    )


def test_a_holder_pushed_during_the_batchs_run_merges_alone_at_its_newhead_of(
    batch_of,
):
    ln, hub, the_line = batch_of(drafts=("7",))
    _during_the_run(ln, hub, lambda: hub.prs[OWN].update(headRefOid="f" * 40))
    _land(ln)
    # The members were stacked on the holder's old head, so none of them merges behind it.
    assert hub.merged == [OWN]
    assert "7" not in hub.ever_labelled
    assert _answers(the_line, "7") == [""]


def test_a_member_that_was_marked_ready_and_did_not_merge_gets_a_full_run(
    batch_of, capsys
):
    ln, hub, the_line = batch_of(drafts=("7", "9"), unmergeable=["7"])
    _land(ln)
    assert hub.merged == [OWN]
    # Its labelled run skipped every job, so it is converted to draft and back unlabelled.
    assert hub.full_runs == ["7"]
    assert hub.labels["7"] == set() and hub.prs["7"]["isDraft"] is False
    assert "9" not in hub.ever_labelled
    assert _answers(the_line, "7", "9") == ["", ""]
    assert "PR #7 gets a full run" in capsys.readouterr().out


def test_a_member_that_stayed_a_draft_only_loses_the_label(batch_of):
    ln, hub, _ = batch_of(drafts=("7",), unmergeable=["7"], stays_draft=True)
    _land(ln)
    assert hub.merged == [OWN]
    # No run started under the label, so there is no skipped run to replace.
    assert hub.labels["7"] == set() and hub.full_runs == []
    assert hub.prs["7"]["isDraft"] is True


def test_a_holder_that_does_not_merge_after_its_batch_gets_a_full_run(batch_of):
    ln, hub, the_line = batch_of(
        drafts=("7",), unmergeable=[OWN], opts={"merge_timeout": 60}
    )
    with pytest.raises(Outcome) as ended:
        _land(ln)
    assert ended.value.verdict == "merge-timeout"
    assert hub.full_runs == [OWN]
    assert hub.labels[OWN] == set()
    assert hub.merged == [] and "7" not in hub.ever_labelled
    assert _answers(the_line, "7") == [""]


def test_a_member_is_not_merged_by_landing_code_older_than_masters(batch_of, capsys):
    ln, hub, _ = batch_of(
        drafts=("7",), fakes=Fakes(stale_code=["scripts/deploy_tools/land.py"])
    )
    _land(ln)
    assert hub.merged == [OWN]
    assert "7" not in hub.ever_labelled
    assert "PR #7 is not merged" in capsys.readouterr().out


def test_a_label_github_refuses_leaves_the_member_to_land_alone(batch_of):
    ln, hub, the_line = batch_of(drafts=("7",), refuse="POST issues/7/labels")
    _land(ln)
    assert hub.merged == [OWN]
    assert hub.full_runs == [] and hub.prs["7"]["isDraft"] is True
    assert _answers(the_line, "7") == [""]


def test_a_bug_in_the_batch_leaves_the_landing_its_own_pr(batch_of, capsys):
    ln, hub, _ = batch_of(drafts=("7",), crash="POST pulls")
    _land(ln)
    assert hub.merged == [OWN]
    assert hub.branches == {}
    assert "7" not in hub.ever_labelled
    assert "the landing batch failed (RuntimeError('a bug in the batch'))" in (
        capsys.readouterr().out
    )
