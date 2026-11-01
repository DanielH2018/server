"""`reap` on one host against a claim made from the other host's checkout (#6840).

Both hosts' operator checkouts make unprefixed branches, so `other_clone_owner` cannot tell a
daniel-server claim from a daniel-box one, and the local `git worktree list` does not name the
peer's worktrees. The fix reads the peer's list through a new `FindingsTools` field,
`peer_worktrees`: called with the register (None for this repo, as `running_batch_holds`
takes it), it returns the `lib.worktrees.Worktree`s the other host's checkout registers. The
fake below accepts any arguments, so only the field name and the returned list are fixed.
"""

import dataclasses

from _findings_fakes import Fakes, build_tools, facts, make_issue

from dev.findings import main
from dev.findings_lib.boundaries import FindingsTools
from dev.findings_lib.issue_model import claim_comment
from lib.worktrees import Worktree

PEER_FIELD = "peer_worktrees"
SERVER_WT = "worktree-issue-6840"
GONE_WT = "worktree-issue-6841"


def test_reap_holds_a_claim_whose_worktree_the_peer_host_registers(monkeypatch, capsys):
    # Unset, both branches read as the running user's own, which is the operator-on-both-hosts
    # case the issue describes rather than the other-clone case #4103 already holds.
    monkeypatch.delenv("CLAUDE_WORKTREE_PREFIX", raising=False)
    assert PEER_FIELD in {f.name for f in dataclasses.fields(FindingsTools)}, (
        f"FindingsTools has no `{PEER_FIELD}` field, so `reap` judges a claim only against "
        "this host's worktrees and releases one made from the peer host's checkout"
    )
    peer_tree = Worktree(
        path=f"/home/ubuntu/server/.claude/worktrees/{SERVER_WT[len('worktree-') :]}",
        head="abc1234",
        branch=SERVER_WT,
        locked=False,
        lock_reason="",
    )
    held = make_issue(6840, comments=[claim_comment(SERVER_WT, None, "t")])
    gone = make_issue(6841, comments=[claim_comment(GONE_WT, None, "t")])
    tools, calls = build_tools(Fakes(issues=[held, gone], worktree_facts=facts()))
    tools = dataclasses.replace(
        tools, **{PEER_FIELD: lambda *_args, **_kwargs: [peer_tree]}
    )

    assert main(["reap"], tools) == 0

    assert ["issue", "edit", "6840", "--remove-label", "claimed"] not in calls.gh
    assert not any(c[:3] == ["issue", "comment", "6840"] for c in calls.gh)
    # The rejecting half: a claim neither host registers is still released.
    assert ["issue", "edit", "6841", "--remove-label", "claimed"] in calls.gh
    assert "reap: 1 stale claim(s) released" in capsys.readouterr().out
