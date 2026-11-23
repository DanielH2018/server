"""How a landing batch treats the landing turn when GitHub refuses a read for its rate.

Run: uv run pytest scripts/deploy_tools/tests/test_land_batch_turn.py

A rate-limit wait gives the turn back for its length (`Landing.without_turn`). A member's wait
ends the batch, and so does a holder's own wait after which it could not take the turn back.
`test_land_batch.py` has how a batch forms, and the same fixture and model.
"""

import subprocess
from dataclasses import replace

from _land_batch_fakes import OWN, answers, land
from _land_fakes import Fakes
from deploy_tools.land_lib import gh_read, pr_json

LIMITED = subprocess.CalledProcessError(
    1, "gh", stderr="gh: You have exceeded a secondary rate limit. (HTTP 403)"
)


def test_a_holder_stacks_its_batch_only_once_it_holds_the_turn_again(batch_of):
    ln, hub, _ = batch_of(drafts=("7",))
    refused = []

    def gh_json(*args, **kwargs):
        # The batch reads master's tip before it stacks on it, and GitHub refuses that once.
        if "repos/{owner}/{repo}/git/ref/heads/master" in args and not refused:
            refused.append(args)
            raise LIMITED
        if args[2:4] == ("POST", "repos/{owner}/{repo}/git/refs"):
            hub.calls.append(("stack", (), {}))
        return hub.gh_json(*args, **kwargs)

    ln.tools = replace(ln.tools, gh_json=gh_json)
    land(ln)
    order = [
        c[0]
        for c in hub.calls
        if c[0] in ("try_turn", "release_turn", "sleep", "stack")
    ]
    assert order[:5] == ["try_turn", "release_turn", "sleep", "try_turn", "stack"]
    assert hub.merged == [OWN, "7"]


def test_a_holder_whose_turn_a_rate_limit_wait_lost_forms_no_batch(batch_of, capsys):
    ln, hub, _ = batch_of(drafts=("7",), fakes=Fakes(turn=[True, False]))
    # The read before the turn fails for good, so the turn's rule reads the files again;
    # GitHub refuses that read too, and after its wait another landing holds the turn.
    refusals = iter(range(gh_read.RATE_LIMIT_WAITS + 2))

    def gh_json(*args, **kwargs):
        if "files,headRefOid" in args and next(refusals, None) is not None:
            raise LIMITED
        return hub.gh_json(*args, **kwargs)

    ln.tools = replace(ln.tools, gh_json=gh_json)
    land(ln)
    assert "going on without a turn" in capsys.readouterr().out
    assert hub.stacked == {} and hub.batch_prs == []
    assert hub.merged == [OWN]


def test_a_members_rate_limit_wait_gives_the_turn_back_and_forms_no_batch(batch_of):
    ln, hub, the_line = batch_of(drafts=("7",))
    refused = []

    def gh_json(*args, **kwargs):
        # The holder reads PR 7's files while it checks 7 for the batch; GitHub refuses once.
        if args[:3] == ("pr", "view", "7") and "files,headRefOid" in args:
            if not refused:
                refused.append(args)
                raise LIMITED
        return hub.gh_json(*args, **kwargs)

    ln.tools = replace(ln.tools, gh_json=gh_json)
    land(ln)
    order = [c[0] for c in hub.calls if c[0] in ("try_turn", "release_turn", "sleep")]
    assert order[:3] == ["try_turn", "release_turn", "sleep"]
    assert hub.stacked == {} and hub.batch_prs == []
    assert hub.merged == [OWN]
    assert answers(the_line, "7") == [""]


def test_a_holder_whose_turn_a_members_wait_lost_leaves_its_branch_alone(
    batch_of, capsys
):
    # Master moved on the PR's path, which a holder of the turn answers with a branch update.
    moved = Fakes(
        turn=[True, False],
        compare=[["docs/notes.md"]],
        pr_files=[{"filename": "docs/notes.md", "status": "modified"}],
    )
    ln, hub, _ = batch_of(drafts=("7",), fakes=moved)
    refused = []

    def gh_json(*args, **kwargs):
        # GitHub refuses PR 7's file read once; after that wait another landing holds the turn.
        if args[:3] == ("pr", "view", "7") and "files,headRefOid" in args:
            if not refused:
                refused.append(args)
                raise LIMITED
        return hub.gh_json(*args, **kwargs)

    ln.tools = replace(ln.tools, gh_json=gh_json)
    land(ln)
    assert "going on without a turn" in capsys.readouterr().out
    updates = [
        args
        for name, args, _ in hub.calls
        if name == "gh" and args[3].endswith("/update-branch")
    ]
    assert updates == []
    assert hub.stacked == {} and hub.merged == [OWN]


def test_a_holder_whose_own_wait_lost_the_turn_stacks_no_batch(batch_of, capsys):
    ln, hub, the_line = batch_of(drafts=("7",), fakes=Fakes(turn=[True, False]))
    refused = []

    def gh_json(*args, **kwargs):
        # GitHub refuses the holder's read of master's tip once; after that wait another
        # landing holds the turn.
        if "repos/{owner}/{repo}/git/ref/heads/master" in args and not refused:
            refused.append(args)
            raise LIMITED
        return hub.gh_json(*args, **kwargs)

    ln.tools = replace(ln.tools, gh_json=gh_json)
    land(ln)
    assert "going on without a turn" in capsys.readouterr().out
    assert hub.stacked == {} and hub.batch_prs == []
    assert hub.merged == [OWN]
    assert answers(the_line, "7") == [""]


def test_a_member_merges_only_once_the_holder_holds_the_turn_again(batch_of):
    ln, hub, _ = batch_of(drafts=("7",))
    refused = []

    def gh_json(*args, **kwargs):
        # The holder reads its own merge commit before the first member's merge.
        if "{merged, sha: .merge_commit_sha}" in args and not refused:
            refused.append(args)
            raise LIMITED
        return hub.gh_json(*args, **kwargs)

    def gh(*args, **kwargs):
        if "PUT" in args and args[3].endswith("/merge"):
            hub.calls.append(("merge", (args[3].split("/")[-2],), {}))
        return hub.gh(*args, **kwargs)

    ln.tools = replace(ln.tools, gh_json=gh_json, gh=gh)
    land(ln)
    order = [
        c[0] if c[0] != "merge" else f"merge {c[1][0]}"
        for c in hub.calls
        if c[0] in ("try_turn", "release_turn", "sleep", "merge")
    ]
    assert order[order.index(f"merge {OWN}") :] == [
        f"merge {OWN}",
        "release_turn",
        "sleep",
        "try_turn",
        "merge 7",
        "release_turn",
    ]
    assert hub.merged == [OWN, "7"]


def test_a_members_rate_limit_wait_in_its_merge_gives_the_turn_back_and_ends_the_batch(
    batch_of,
):
    ln, hub, the_line = batch_of(drafts=("7", "9"))
    polled = pr_json.rest_filter("state,mergeable,headRefOid")
    refused = []

    def gh_json(*args, **kwargs):
        # PR 7's merge wait polls it once it is labelled and ready; GitHub refuses that once.
        ready = not hub.prs["7"]["isDraft"]
        if ready and "repos/{owner}/{repo}/pulls/7" in args and polled in args:
            if not refused:
                refused.append(args)
                raise LIMITED
        return hub.gh_json(*args, **kwargs)

    def gh(*args, **kwargs):
        if "PUT" in args and args[3].endswith("/merge"):
            hub.calls.append(("merge", (args[3].split("/")[-2],), {}))
        return hub.gh(*args, **kwargs)

    ln.tools = replace(ln.tools, gh_json=gh_json, gh=gh)
    land(ln)
    order = [
        c[0] if c[0] != "merge" else f"merge {c[1][0]}"
        for c in hub.calls
        if c[0] in ("try_turn", "release_turn", "sleep", "merge")
    ]
    assert order[order.index(f"merge {OWN}") :] == [
        f"merge {OWN}",
        "release_turn",
        "sleep",
        "try_turn",
        "release_turn",
    ]
    assert hub.merged == [OWN]
    # PR 7 was labelled and marked ready, so it gets a full run before anything lands it.
    assert hub.full_runs == ["7"] and hub.labels["7"] == set()
    assert answers(the_line, "7", "9") == ["", ""]


def test_a_holder_whose_own_wait_lost_the_turn_merges_no_member(batch_of, capsys):
    ln, hub, the_line = batch_of(drafts=("7",), fakes=Fakes(turn=[True, False]))
    refused = []

    def gh_json(*args, **kwargs):
        # GitHub refuses the holder's read of its own merge commit once; after that wait
        # another landing holds the turn.
        if "{merged, sha: .merge_commit_sha}" in args and not refused:
            refused.append(args)
            raise LIMITED
        return hub.gh_json(*args, **kwargs)

    ln.tools = replace(ln.tools, gh_json=gh_json)
    land(ln)
    assert "going on without a turn" in capsys.readouterr().out
    assert hub.merged == [OWN]
    assert "7" not in hub.ever_labelled
    assert answers(the_line, "7") == [""]
