"""The other host's worktree list, and the claims it holds when this host has no worktree (#6840).

`test_findings_reap_peer_host.py` covers `reap` holding a claim the peer registers. This module
covers the reader itself, the failed-read direction, and `claim`'s reap on the way past.

Run: uv run pytest scripts/dev/tests/test_findings_peer_trees.py
"""

import dataclasses
import subprocess

from _findings_fakes import Fakes, build_tools, facts, make_issue

from dev.findings import main
from dev.findings_lib.issue_model import claim_comment
from dev.findings_lib.peer_trees import peer_worktree_list
from lib.worktrees import Worktree

MINE = "worktree-mine"
OTHER = "worktree-issue-1132"
MINE_TREE = Worktree(
    path="/w/mine", head="abc1234", branch=MINE, locked=True, lock_reason="held"
)
PORCELAIN = (
    "worktree /home/ubuntu/server\nHEAD aaa\nbranch refs/heads/master\n\n"
    f"worktree /home/ubuntu/server/.claude/worktrees/issue-1132\nHEAD bbb\n"
    f"branch refs/heads/{OTHER}\n"
)


def _run(stdout="", returncode=0, raises=None):
    seen = []

    def run(host, command, timeout):
        seen.append((host, command))
        if raises:
            raise raises
        return subprocess.CompletedProcess([], returncode, stdout, "")

    return run, seen


def test_the_reader_lists_the_other_hosts_worktrees():
    run, seen = _run(PORCELAIN)
    trees = peer_worktree_list("/home/ubuntu/my server", run, local_host="daniel-box")
    assert trees is not None
    assert [t.branch for t in trees] == ["master", OTHER]
    assert seen == [
        ("daniel-server", "git -C '/home/ubuntu/my server' worktree list --porcelain")
    ]


def test_a_host_with_no_peer_reads_nothing():
    run, seen = _run(PORCELAIN)
    assert peer_worktree_list("/c", run, local_host="ci-runner") == []
    assert seen == []


def test_a_failed_or_empty_peer_read_is_none_not_an_empty_list():
    for run, _ in (
        _run(returncode=255),
        _run(""),
        _run(raises=subprocess.TimeoutExpired("ssh", 15)),
    ):
        assert peer_worktree_list("/c", run, local_host="daniel-server") is None


def _peer(trees):
    """A `peer_worktrees` fake returning ``trees``, and the list each call appends to."""
    calls = []

    def read(*args):
        calls.append(args)
        return trees

    return read, calls


def test_reap_holds_a_claim_with_no_local_worktree_when_the_peer_cannot_be_read(capsys):
    issue = make_issue(1132, comments=[claim_comment(OTHER, None, "t")])
    tools, calls = build_tools(Fakes(issues=[issue], worktree_facts=facts()))
    tools = dataclasses.replace(tools, peer_worktrees=_peer(None)[0])
    assert main(["reap"], tools) == 0
    assert not any("--remove-label" in c for c in calls.gh)
    assert "reap: 0 stale claim(s) released" in capsys.readouterr().out


def test_reap_reads_no_peer_when_every_stale_claim_has_a_local_worktree():
    tree = Worktree(f"/w/{OTHER}", "abc1234", OTHER, False, "")
    issue = make_issue(1132, comments=[claim_comment(OTHER, None, "t")])
    tools, calls = build_tools(
        Fakes(issues=[issue], worktree_facts=facts([tree], merged=True))
    )
    peer, peer_calls = _peer([tree])
    tools = dataclasses.replace(tools, peer_worktrees=peer)
    assert main(["reap"], tools) == 0
    assert peer_calls == []
    assert ["issue", "edit", "1132", "--remove-label", "claimed"] in calls.gh


def test_claim_does_not_reap_a_claim_the_peer_registers(capsys):
    issue = make_issue(1132, comments=[claim_comment(OTHER, None, "t")])
    tools, calls = build_tools(
        Fakes(issues=[issue], view=issue, worktree_facts=facts([MINE_TREE]))
    )
    peer_tree = Worktree(f"/w/{OTHER}", "abc1234", OTHER, False, "")
    tools = dataclasses.replace(tools, peer_worktrees=_peer([peer_tree])[0])
    assert main(["claim", "1132", "--worktree", MINE], tools) == 3
    assert f"already claimed by `{OTHER}`" in capsys.readouterr().err
    assert not any("Released: `" in a for c in calls.gh for a in c)
