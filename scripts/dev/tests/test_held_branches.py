"""Tests for `held_branches.py`: the branches the worktree pruner must not delete here.

The pruner runs the script as its `branch_holds` command and keeps every branch it prints. A
register that cannot be read has to fail the script, because the pruner reads exit 0 with no
output as "nothing is held".

Run: uv run pytest scripts/dev/tests/test_held_branches.py
"""

from pathlib import Path

import pytest

from dev.held_branches import claimed_branches, main, open_pr_heads
from lib.proc_testing import fake_bin, path_with


def _findings_stub(root: Path, body: str) -> None:
    script = root / "scripts" / "dev" / "findings.py"
    script.parent.mkdir(parents=True)
    script.write_text(body)


def _gh(monkeypatch, tmp_path: Path, body: str) -> None:
    """Put a `gh` that runs `body` first on PATH."""
    monkeypatch.setenv("PATH", path_with(fake_bin(tmp_path / "bin", gh=body)))


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


def test_open_pr_heads_names_the_head_of_every_open_pr(tmp_path, monkeypatch):
    _gh(monkeypatch, tmp_path, "printf 'claude/open\\nworktree-open\\n'\n")

    assert open_pr_heads(str(tmp_path)) == {"claude/open", "worktree-open"}


def test_open_pr_heads_is_unread_when_gh_fails(tmp_path, monkeypatch):
    _gh(monkeypatch, tmp_path, "exit 1\n")

    assert open_pr_heads(str(tmp_path)) is None


def test_open_pr_heads_is_unread_when_the_list_is_cut_off(tmp_path, monkeypatch):
    # A list at the limit may be missing the one branch that matters.
    _gh(monkeypatch, tmp_path, "printf 'a\\nb\\n'\n")

    assert open_pr_heads(str(tmp_path), limit=2) is None


def test_main_prints_the_branches_either_register_holds(capsys):
    registers = (
        ("the open PRs", lambda repo: {"claude/pr"}),
        ("the claims", lambda repo: {"worktree-claim", "claude/pr"}),
    )

    assert main([], registers) == 0
    assert capsys.readouterr().out.split() == ["claude/pr", "worktree-claim"]


@pytest.mark.parametrize("named", ["the open PRs", "the claims"])
def test_main_fails_and_prints_no_branch_when_a_register_is_unread(capsys, named):
    readers = {
        "the open PRs": lambda repo: {"claude/pr"},
        "the claims": lambda repo: set(),
    }
    readers[named] = lambda repo: None

    assert main([], tuple(readers.items())) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"{named} could not be read" in captured.err
