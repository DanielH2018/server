#!/usr/bin/env python3
"""Tests for which session branches `prune_worktrees.py` deletes: `judge_branches` and `--check`.

Real git throughout. The forge is `GH_BIN`: `true` answers every read with nothing, which is a
forge with no PRs, and `false` fails every read.

Run: uv run pytest scripts/dev/tests/test_prune_worktrees_branches.py
"""

import os
import subprocess
from pathlib import Path

import pytest

from dev.prune_worktrees import (
    CLAIMED,
    CLAIMS_UNREAD,
    HELD,
    OPEN_PR,
    OPEN_PRS_UNREAD,
    UNLANDED,
    claimed_branches,
    judge_branches,
    main,
)
from lib.git_testing import commit, git, git_out, init_repo, scrub_process_git_env
from lib.worktree_testing import ignore_foreign_slice_holds
from lib.worktrees import merged_layer


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    """A scratch repo with a landed orphan, an unlanded and a held `claude/` branch.

    The held worktree is dirty, so a prune keeps it and its branch."""
    scrub_process_git_env(monkeypatch)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    monkeypatch.setenv("GH_BIN", "true")
    ignore_foreign_slice_holds(monkeypatch)
    repo = tmp_path / "repo"
    init_repo(repo)
    commit(repo, "init", **{"a.txt": "one\n"})
    git(repo, "branch", "claude/landed")
    git(repo, "checkout", "-q", "-b", "claude/unlanded")
    commit(repo, "open work", **{"b.txt": "two\n"})
    git(repo, "checkout", "-q", "master")
    git(repo, "worktree", "add", "-q", "-b", "claude/held", str(tmp_path / "held"))
    (tmp_path / "held" / "wip.txt").write_text("uncommitted\n")
    git(repo, "update-ref", "refs/remotes/origin/master", "master")
    monkeypatch.chdir(repo)
    return repo


def _run(capsys, *argv: str) -> subprocess.CompletedProcess:
    capsys.readouterr()
    code = main(list(argv))
    out, err = capsys.readouterr()
    return subprocess.CompletedProcess(argv, code, out, err)


def _branches(repo: Path) -> list[str]:
    return git_out(repo, "branch", "--format=%(refname:short)").split()


def _judged(repo: Path, **seams) -> dict[str, tuple[bool, str]]:
    return {v.branch: (v.delete, v.reason) for v in judge_branches(str(repo), **seams)}


def test_a_prune_deletes_the_landed_orphan_and_keeps_the_held_and_the_unlanded(
    repo, capsys
):
    done = _run(capsys, "--prune")

    assert done.returncode == 0, done.stderr
    assert _branches(repo) == ["claude/held", "claude/unlanded", "master"]


def test_a_prune_keeps_a_landed_orphan_when_the_forge_cannot_be_read(
    repo, monkeypatch, capsys
):
    monkeypatch.setenv("GH_BIN", "false")

    done = _run(capsys, "--prune")

    assert done.returncode == 0, done.stderr
    assert "claude/landed" in _branches(repo)
    assert f"kept 1 landed branch(es): {OPEN_PRS_UNREAD}" in done.stderr


@pytest.mark.parametrize(
    ("seam", "answer", "reason"),
    [
        ("open_prs", {"claude/landed"}, OPEN_PR),
        ("claimed", {"claude/landed"}, CLAIMED),
        ("claimed", None, CLAIMS_UNREAD),
    ],
)
def test_a_landed_orphan_a_register_names_or_cannot_be_read_for_is_kept(
    repo, seam, answer, reason
):
    # fact: .claude/skills/worktree-cleanup/SKILL.md#The pruner
    assert _judged(repo)["claude/landed"] == (True, "landed by ancestry")
    assert _judged(repo, **{seam: lambda _repo: answer})["claude/landed"] == (
        False,
        reason,
    )


@pytest.mark.parametrize(
    ("forge_answer", "verdict"),
    [(True, (True, "landed by forge")), (False, (False, UNLANDED))],
)
def test_a_branch_no_local_layer_settles_is_deleted_only_on_the_forges_word(
    repo, forge_answer, verdict
):
    # claude/unlanded stands for a squash-merged branch master drifted from. A forge read
    # that failed answers False, the same as "not merged".
    def layer(repo, head, branch):
        return merged_layer(repo, head, branch, forge=lambda *_: forge_answer)

    assert _judged(repo, layer=layer)["claude/unlanded"] == verdict


def _findings_stub(root: Path, body: str) -> None:
    script = root / "scripts" / "dev" / "findings.py"
    script.parent.mkdir(parents=True)
    script.write_text(body)


def test_claimed_branches_names_every_claims_branch_stale_ones_too(tmp_path):
    _findings_stub(
        tmp_path,
        "import json\n"
        "row = dict(number=1, live=True, reason='', age_days=None)\n"
        "print(json.dumps([dict(row, worktree='claude/a'),"
        " dict(row, worktree='worktree-b', live=False)]))\n",
    )

    assert claimed_branches(str(tmp_path)) == {"claude/a", "worktree-b"}


def test_claimed_branches_is_unread_when_the_claims_command_fails(tmp_path):
    _findings_stub(tmp_path, "import sys\nprint('[]')\nsys.exit(1)\n")

    assert claimed_branches(str(tmp_path)) is None


def test_a_bare_check_lists_the_deletions_and_counts_the_kept(repo, capsys):
    git(repo, "branch", "worktree-landed")

    done = _run(capsys, "--check")

    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [
        "4 session branch(es): 2 would be deleted (landed by ancestry), "
        f"1 kept ({HELD}), 1 kept ({UNLANDED}); --all lists the kept",
        "worktree-*: 1 would be deleted (landed by ancestry)",
        "claude/*: 1 would be deleted (landed by ancestry), "
        f"1 kept ({HELD}), 1 kept ({UNLANDED})",
        "would be deleted (landed by ancestry):",
        "  claude/landed",
        "  worktree-landed",
    ]
    assert "claude/landed" in _branches(repo)
