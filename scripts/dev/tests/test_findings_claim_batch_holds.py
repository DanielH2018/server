"""A dead orchestrator's claims stay live while a batch it launched is still running (#4601).

Every fixture holds the claim under an orchestrator worktree whose lock owner is dead, clean
and merged: `classify` calls it REMOVABLE, so the worktree alone reads every claim stale. The
running-batch read is what decides, and each `_is_held` test has an `_is_stale` twin where the
batch has finished, so a read that holds everything and one that holds nothing both fail.

Run: uv run pytest scripts/dev/tests/test_findings_claim_batch_holds.py
"""

import json

from _findings_fakes import Fakes, build_tools, facts, make_issue

from dev.findings import main
from dev.findings_lib.issue_model import claim_comment
from lib.worktrees import Worktree

MINE = "worktree-mine"
ORCH = "worktree-fanout-orchestrator-backlog"
RUNNING = {1132: "fan-out batch 1132 is still running (u on daniel-box)"}

# A session that can claim: its lock reason does not parse, which `session_is_alive` reads live.
MINE_TREE = Worktree(
    path="/w/mine", head="abc", branch=MINE, locked=True, lock_reason="held"
)
DEAD_ORCH = Worktree(
    path="/w/orch",
    head="abc",
    branch=ORCH,
    locked=True,
    lock_reason="claude session x (pid 999999 start 999999)",
)
DEAD_ORCH_FACTS = facts([MINE_TREE, DEAD_ORCH], merged=True)


def _tools(holds):
    issue = make_issue(1132, comments=[claim_comment(ORCH, None, "t")])
    return build_tools(
        Fakes(
            issues=[issue],
            view=issue,
            worktree_facts=DEAD_ORCH_FACTS,
            batch_holds=holds,
        )
    )


def _released(calls) -> bool:
    return any("Released: `" in a for c in calls.gh for a in c)


def test_next_withholds_an_issue_a_running_batch_holds_is_held(capsys):
    tools, calls = _tools(RUNNING)
    assert main(["next", "--json"], tools) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert calls.batch_holds == [(None, [1132])]


def test_next_offers_the_issue_once_its_batch_has_finished_is_stale(capsys):
    tools, _ = _tools({})
    assert main(["next", "--json"], tools) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [(r["number"], r["stale_claim_by"]) for r in rows] == [(1132, ORCH)]


def test_claim_refuses_an_issue_a_running_batch_holds_is_held(capsys):
    tools, calls = _tools(RUNNING)
    assert main(["claim", "1132", "--worktree", MINE], tools) == 3
    assert f"already claimed by `{ORCH}`" in capsys.readouterr().err
    assert not _released(calls)


def test_claim_reaps_the_issue_once_its_batch_has_finished_is_stale(capsys):
    tools, calls = _tools({})
    assert main(["claim", "1132", "--worktree", MINE], tools) == 0
    assert f"#1132 claimed by `{MINE}`" in capsys.readouterr().out
    assert _released(calls)


def test_reap_leaves_a_claim_a_running_batch_holds_is_held(capsys):
    tools, calls = _tools(RUNNING)
    assert main(["reap"], tools) == 0
    assert "0 stale claim(s) released" in capsys.readouterr().out
    assert not _released(calls)


def test_reap_releases_the_claim_once_its_batch_has_finished_is_stale(capsys):
    tools, calls = _tools({})
    assert main(["reap"], tools) == 0
    assert "1 stale claim(s) released" in capsys.readouterr().out
    assert _released(calls)


def test_claims_names_the_running_batch_as_the_reason(capsys):
    tools, _ = _tools(RUNNING)
    assert main(["claims"], tools) == 0
    out = capsys.readouterr().out
    assert "live " in out and RUNNING[1132] in out


def test_a_live_worktree_claim_never_reads_the_batches():
    # The cheap read decides first: with no stale claim there is nothing to ask a host.
    issue = make_issue(1132, comments=[claim_comment(MINE, None, "t")])
    tools, calls = build_tools(
        Fakes(issues=[issue], worktree_facts=facts([MINE_TREE]), batch_holds=RUNNING)
    )
    assert main(["next", "--json"], tools) == 0
    assert calls.batch_holds == []
