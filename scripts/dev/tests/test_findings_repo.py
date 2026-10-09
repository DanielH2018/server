"""`--repo` on the register subcommands: every gh call and the worktree read aim at that repo.

`open --repo` is covered beside the rest of `open`, in test_findings_open.py. This module
covers the subcommands that gained the flag for #3345, and the prune_worktrees read they
reach through it: a checkout whose default branch is not origin/master.
"""

import json
from pathlib import Path

from _findings_fakes import Fakes, build_tools, facts, live_worktree, make_issue
from lib.git_testing import commit, git, init_repo, scrub_process_git_env

from dev.findings import main
from dev.findings_lib.boundaries import REGISTER_CHECKOUTS
from dev.findings_lib.issue_model import claim_comment
from lib.worktrees import worktree_facts, is_merged

WT = "worktree-fanout-750"
DOTFILES = "DanielH2018/dotfiles"


def _all_aimed(calls) -> bool:
    return all(c[-2:] == ["--repo", DOTFILES] for c in calls.gh + calls.gh_json)


def test_claim_with_repo_aims_every_call_and_the_worktree_read_there():
    """A dotfiles claim names a branch in the chezmoi checkout, so that is where its
    staleness is read; every gh call carries the flag, the label sync and read-back too."""
    issue = make_issue(750)
    tools, calls = build_tools(
        Fakes(issues=[issue], view=issue, worktree_facts=live_worktree(WT))
    )
    assert main(["claim", "750", "--worktree", WT, "--repo", DOTFILES], tools) == 0
    assert calls.checkouts == [REGISTER_CHECKOUTS[DOTFILES]]
    assert any(c[:2] == ["issue", "comment"] for c in calls.gh)
    assert _all_aimed(calls)


def test_claim_without_repo_reads_this_repos_worktrees():
    """The rejecting half: the default names no repo and reads no other checkout."""
    issue = make_issue(1132)
    tools, calls = build_tools(
        Fakes(issues=[issue], view=issue, worktree_facts=live_worktree(WT))
    )
    assert main(["claim", "1132", "--worktree", WT], tools) == 0
    assert calls.checkouts == [None]
    assert not any("--repo" in c for c in calls.gh + calls.gh_json)


def test_release_and_close_with_repo_aim_every_call_there():
    for argv in (
        ["release", "750", "--worktree", WT, "--repo", DOTFILES],
        ["close", "750", "--fixed", "--pr", "760", "--repo", DOTFILES],
    ):
        # Fresh per command: the fake reflects a posted release onto the issue it was given.
        issue = make_issue(
            750, labels=["claimed"], comments=[claim_comment(WT, None, "t")]
        )
        tools, calls = build_tools(Fakes(issues=[issue], view=issue))
        assert main(argv, tools) == 0, argv
        unlabel = ["issue", "edit", "750", "--remove-label", "claimed"]
        assert [*unlabel, "--repo", DOTFILES] in calls.gh
        assert _all_aimed(calls)


def test_next_with_repo_reads_the_open_prs_of_that_repo(capsys):
    """Issue numbers collide across repos: a server PR saying `Closes #750` must not
    withhold dotfiles #750, so the PR read is aimed with the issue read."""
    tools, calls = build_tools(
        Fakes(issues=[make_issue(750)], prs=[], worktree_facts=facts())
    )
    assert main(["next", "--json", "--repo", DOTFILES], tools) == 0
    assert [r["number"] for r in json.loads(capsys.readouterr().out)] == [750]
    assert ["pr", "list"] in [c[:2] for c in calls.gh_json]
    assert _all_aimed(calls)


def test_a_worktree_reading_subcommand_refuses_a_repo_with_no_known_checkout(capsys):
    """Judged against this repo's worktrees, every claim on the unknown repo would read
    stale, and `reap` would release all of them."""
    for argv in (["reap"], ["claims"], ["next"], ["claim", "1", "--worktree", WT]):
        tools, calls = build_tools(Fakes(worktree_facts=live_worktree(WT)))
        assert main([*argv, "--repo", "someone/else"], tools) == 2, argv
        assert calls.none() and not calls.checkouts
    assert "no local checkout" in capsys.readouterr().err


# --- the worktree read against another checkout's default branch ---------------------------


def _repo_on_main(tmp_path: Path, *, symref: bool) -> Path:
    """A scratch repo whose default branch is `main`, as the dotfiles repo's is, with one
    worktree whose branch sits at main's tip."""
    repo = tmp_path / "repo"
    init_repo(repo, branch="main")
    commit(repo, "init", **{"a.txt": "one\n"})
    if symref:
        git(repo, "update-ref", "refs/remotes/origin/main", "main")
        git(
            repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"
        )
    git(repo, "worktree", "add", "-q", "-b", "worktree-landed", str(tmp_path / "wt"))
    return repo


def testworktree_facts_for_a_checkout_merges_against_its_default_branch(
    tmp_path, monkeypatch
):
    """Judged against origin/master, a landed tree in the dotfiles checkout would never
    read merged, and no claim on its register could go stale."""
    scrub_process_git_env(monkeypatch)
    repo = _repo_on_main(tmp_path, symref=True)
    trees, _dirty, merged, ok = worktree_facts(str(repo))
    assert ok is True
    tree = next(t for t in trees if t.branch == "worktree-landed")
    assert merged(tree) is True
    assert is_merged(str(repo), tree.head, "", base="origin/master") is False


def testworktree_facts_for_a_checkout_with_no_default_branch_is_a_failed_read(
    tmp_path, monkeypatch
):
    """The rejecting half: with no merge target nothing can be shown landed, and `reap` must
    refuse rather than judge every claim on that register."""
    scrub_process_git_env(monkeypatch)
    repo = _repo_on_main(tmp_path, symref=False)
    trees, _dirty, _merged, ok = worktree_facts(str(repo))
    assert ok is False
    assert trees == []
