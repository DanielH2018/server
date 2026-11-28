"""What the landing that holds the turn does with the members of a green batch.

Run: uv run pytest scripts/deploy_tools/tests/land_merge/test_land_batch_merges.py

`test_land_batch.py` has how a batch forms, and the same fixture and model.
"""

import json
from dataclasses import replace

import pytest

from _land_batch_fakes import GREEN, OWN, answers, head_of, land
from deploy_tools.land_lib import batch, line
from deploy_tools.land_lib.outcome import Outcome
from deploy_tools.land_lib.tools import CiVerdict
from lib.exit_codes import CI_PENDING


def test_a_member_whose_landing_left_the_line_ends_the_batch_there(batch_of, capsys):
    ln, hub, the_line = batch_of(drafts=("5", "7", "9"))
    real = ln.tools.line

    def leaves_during_the_run(op, *args):
        # PR 7's landing gives up while the holder merges the PRs before it.
        if op == "answer" and args[0] == "5":
            the_line("leave", "7")
        return real(op, *args)

    ln.tools = replace(ln.tools, line=leaves_during_the_run)
    land(ln)
    assert hub.merged == [OWN, "5"]
    # Neither it nor the PR stacked on it was ever labelled, so each run alone is a full one.
    assert hub.ever_labelled == {OWN, "5"}
    assert answers(the_line, "9") == [""]
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
    land(ln)
    assert hub.merged == [OWN, "7"]
    assert "9" not in hub.ever_labelled
    assert answers(the_line, "7", "9") == [line.MERGED, ""]
    out = capsys.readouterr().out
    assert "the batch's run no longer stands for PR #9: master is eeeeeeee" in out
    assert "PR #9 is not merged" in out


def test_master_moving_during_the_batchs_run_leaves_the_holder_to_the_stale_rules(
    batch_of, capsys
):
    ln, hub, the_line = batch_of(drafts=("7",))
    real = ln.tools.await_ci

    def master_moves_during_the_run(sha, timeout, on_checks):
        if sha == hub.branches.get(f"queue/{OWN}"):
            hub.master = "e" * 40
        return real(sha, timeout, on_checks)

    ln.tools = replace(ln.tools, await_ci=master_moves_during_the_run)
    land(ln)
    # The holder merges as any green head does, and the member is not merged behind it.
    assert hub.merged == [OWN]
    assert "7" not in hub.ever_labelled
    assert answers(the_line, "7") == [""]
    out = capsys.readouterr().out
    assert "the stale-base rules judge it" in out
    assert "the batch's run no longer stands, so its other PRs land alone" in out


def _during_the_run(ln, hub, change) -> None:
    """Run `change` when the landing first reads the batch's run."""
    real = ln.tools.await_ci
    done = []

    def await_ci(sha, timeout, on_checks):
        if sha == hub.branches.get(f"queue/{OWN}") and not done:
            done.append(change())
        return real(sha, timeout, on_checks)

    ln.tools = replace(ln.tools, await_ci=await_ci)


def test_a_member_pushed_during_the_batchs_run_is_not_merged(batch_of, capsys):
    ln, hub, the_line = batch_of(drafts=("7", "9"))
    _during_the_run(ln, hub, lambda: hub.prs["7"].update(headRefOid="f" * 40))
    land(ln)
    # The batch tested its old head, so neither it nor the PR stacked on it is labelled.
    assert hub.merged == [OWN]
    assert hub.ever_labelled == {OWN}
    assert answers(the_line, "7", "9") == ["", ""]
    assert "its head is ffffffff, not the 00000000 the batch stacked" in (
        capsys.readouterr().out
    )


def test_a_holder_pushed_during_the_batchs_run_merges_alone_at_its_newhead_of(
    batch_of,
):
    ln, hub, the_line = batch_of(drafts=("7",))
    _during_the_run(ln, hub, lambda: hub.prs[OWN].update(headRefOid="f" * 40))
    land(ln)
    # The members were stacked on the holder's old head, so none of them merges behind it.
    assert hub.merged == [OWN]
    assert "7" not in hub.ever_labelled
    assert answers(the_line, "7") == [""]


def test_a_member_that_was_marked_ready_and_did_not_merge_gets_a_full_run(
    batch_of, capsys
):
    ln, hub, the_line = batch_of(drafts=("7", "9"), unmergeable=["7"])
    land(ln)
    assert hub.merged == [OWN]
    # Its labelled run skipped every job, so it is converted to draft and back unlabelled.
    assert hub.full_runs == ["7"]
    assert hub.labels["7"] == set() and hub.prs["7"]["isDraft"] is False
    assert "9" not in hub.ever_labelled
    assert answers(the_line, "7", "9") == ["", ""]
    assert "PR #7 gets a full run" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("poll", "merged"),
    [
        pytest.param(20, [OWN, "7"], id="room-left"),
        # They leave 1100 s, which is less than that.
        pytest.param(100, [OWN], id="budgets-spent"),
    ],
)
def test_the_members_merge_inside_the_landings_two_merge_budgets(
    batch_of, poll, merged
):
    ln, hub, _ = batch_of(
        drafts=("7",),
        run=[(CI_PENDING, "pending")] * 3 + [GREEN],
        opts={"merge_timeout": 700, "merge_poll": poll},
    )
    land(ln)
    assert hub.merged == merged
    assert ("7" in hub.ever_labelled) == ("7" in merged)


def test_only_the_holder_writes_a_state_record_counting_the_members_it_merged(
    batch_of,
):
    ln, hub, _ = batch_of(drafts=("5", "7"))
    land(ln)
    assert hub.merged == [OWN, "5", "7"]
    records = [json.loads(c[1][1]) for c in hub.calls if c[0] == "record"]
    # Each member's own landing writes its PR's record, so the holder writes none for it.
    assert {c[1][0] for c in hub.calls if c[0] == "record"} == {OWN}
    assert [r["progress"] for r in records if r["progress"]] == [
        {"done": done, "total": 2, "what": "batch members merged"} for done in (0, 1, 2)
    ]


def test_a_slow_member_leaves_no_budget_for_the_one_behind_it(batch_of):
    ln, hub, the_line = batch_of(
        drafts=("7", "9"), opts={"merge_timeout": 850, "merge_poll": 110}
    )
    # PR 7's short run takes five polls, 550 s of the 1700 s the two budgets hold.
    pending = [CiVerdict(CI_PENDING, "pending")] * 5
    its_ci = ln.tools.await_ci

    def await_ci(sha, timeout, on_checks):
        if sha == head_of("7") and not hub.prs["7"]["isDraft"] and pending:
            return pending.pop()
        return its_ci(sha, timeout, on_checks)

    ln.tools = replace(ln.tools, await_ci=await_ci)
    land(ln)
    assert hub.merged == [OWN, "7"]
    # PR 9 is neither labelled nor answered, so its own landing lands it.
    assert "9" not in hub.ever_labelled
    assert answers(the_line, "9") == [""]


def test_a_member_that_stayed_a_draft_only_loses_the_label(batch_of):
    ln, hub, _ = batch_of(drafts=("7",), unmergeable=["7"], stays_draft=True)
    land(ln)
    assert hub.merged == [OWN]
    # No run started under the label, so there is no skipped run to replace.
    assert hub.labels["7"] == set() and hub.full_runs == []
    assert hub.prs["7"]["isDraft"] is True


def test_a_holder_that_does_not_merge_after_its_batch_gets_a_full_run(batch_of):
    ln, hub, the_line = batch_of(
        drafts=("7",),
        unmergeable=[OWN],
        opts={"merge_timeout": batch.MEMBER_TIMEOUT_S},
    )
    with pytest.raises(Outcome) as ended:
        land(ln)
    assert ended.value.verdict == "merge-timeout"
    assert hub.full_runs == [OWN]
    assert hub.labels[OWN] == set()
    assert hub.merged == [] and "7" not in hub.ever_labelled
    assert answers(the_line, "7") == [""]


def test_a_member_merges_after_a_batch_merge_that_changed_landing_code(batch_of):
    ln, hub, _ = batch_of(drafts=("7", "9"))
    # The holder's own PR changes a module the landing runs.
    ln.tools = replace(
        ln.tools,
        stale_code=lambda base: ["scripts/lib/exit_codes.py"] if hub.merged else [],
    )
    land(ln)
    assert hub.merged == [OWN, "7", "9"]


def test_a_label_github_refuses_leaves_the_member_to_land_alone(batch_of):
    ln, hub, the_line = batch_of(drafts=("7",), refuse="POST issues/7/labels")
    land(ln)
    assert hub.merged == [OWN]
    assert hub.full_runs == [] and hub.prs["7"]["isDraft"] is True
    assert answers(the_line, "7") == [""]
