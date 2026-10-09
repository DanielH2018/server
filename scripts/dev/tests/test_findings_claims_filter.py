"""`claims --worktree`: one orchestrator's own claims, and the run manifests that widen them."""

import json

from _findings_fakes import Fakes, build_tools, facts, make_issue

from dev.fanout_lib.manifest import Batch, Manifest, branches_launched_by, save
from dev.findings import main
from dev.findings_lib.issue_model import claim_comment

ORCH = "worktree-issue-fanout-wave1"
DOTFILES = "DanielH2018/dotfiles"


def _held(number: int, worktree: str) -> dict:
    return make_issue(number, comments=[claim_comment(worktree, None, "t")])


def _batch(branch: str, *, repo: str = "DanielH2018/server", removed_at=None) -> Batch:
    return Batch("1", "daniel-box", "/w", branch, "u", [1], "t", removed_at, repo)


def test_claims_worktree_keeps_its_own_rows_and_counts_the_rest(capsys):
    issues = [_held(1, ORCH), _held(2, "worktree-someone-else"), _held(3, ORCH)]
    tools, _ = build_tools(Fakes(issues=issues, worktree_facts=facts()))
    assert main(["claims", "--worktree", ORCH], tools) == 0
    out = capsys.readouterr().out
    assert "#1 " in out and "#3 " in out
    assert "#2 " not in out
    assert "1 claim(s) held by other worktrees" in out


def test_claims_worktree_includes_the_batch_branches_its_runs_launched(capsys):
    # A dotfiles batch is claimed under its own branch, not the orchestrator's, so only the
    # manifest ties it back. The read is keyed by the aimed register.
    issues = [_held(7, "worktree-fanout-7"), _held(8, "worktree-fanout-8")]
    tools, calls = build_tools(
        Fakes(
            issues=issues,
            worktree_facts=facts(),
            launched={(ORCH, DOTFILES): {"worktree-fanout-7"}},
        )
    )
    assert (
        main(["claims", "--worktree", ORCH, "--repo", DOTFILES, "--json"], tools) == 0
    )
    captured = capsys.readouterr()
    assert [r["number"] for r in json.loads(captured.out)] == [7]
    assert calls.launched == [(ORCH, DOTFILES)]
    # The dropped row is named on stderr so the array stays the filtered set.
    assert "1 claim(s) held by other worktrees" in captured.err


def test_claims_worktree_names_the_filter_when_it_matches_nothing(capsys):
    tools, _ = build_tools(Fakes(issues=[_held(1, "other")], worktree_facts=facts()))
    assert main(["claims", "--worktree", ORCH], tools) == 0
    out = capsys.readouterr().out
    assert f"no open claims held by `{ORCH}`" in out
    assert "1 claim(s) held by other worktrees" in out


def test_branches_launched_by_reads_only_standing_batches_of_that_orchestrator_and_repo(
    tmp_path,
):
    save(
        Manifest(
            "20261009T000001Z",
            ORCH,
            [
                _batch("worktree-fanout-1", repo=DOTFILES),
                _batch("worktree-fanout-2", repo=DOTFILES, removed_at="t"),
                _batch("worktree-fanout-3"),
            ],
        ),
        root=tmp_path,
    )
    save(
        Manifest(
            "20261009T000002Z",
            "someone-else",
            [_batch("worktree-fanout-4", repo=DOTFILES)],
        ),
        root=tmp_path,
    )
    (tmp_path / "20261009T000003Z.json").write_text("{not json")
    assert branches_launched_by(ORCH, DOTFILES, root=tmp_path) == {"worktree-fanout-1"}
    assert branches_launched_by(ORCH, DOTFILES, root=tmp_path / "absent") == set()
