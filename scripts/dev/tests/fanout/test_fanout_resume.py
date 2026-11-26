"""The unit `fanout.py place resume` starts: what its one command runs, and from where.

The command's checks and refusals are in `test_fanout_abandon.py`, and its lock and cap in
`test_fanout_launch_gates.py`.

Run: uv run pytest scripts/dev/tests/fanout/test_fanout_resume.py
"""

from fnmatch import fnmatch
from pathlib import Path

import pytest

from dev.fanout_lib.launch import SNAPSHOT_PATHS, SYSTEM_PROMPT_FILE
from dev.fanout_lib.resume import RESUME_SNAPSHOT_PATHS, resume_command
from dev.fanout_lib.review.held_hooks import HELD_TREES, HOOKS_DIR, RULES_DIR
from _fanout_fakes import as_operator
from lib.doc_freshness import tracked_files
from lib.repo_paths import REPO


def test_the_resume_command_runs_masters_pipeline_from_a_fresh_snapshot_of_it():
    cmd = as_operator(resume_command("7", "fanout-7"))
    snapshot = "/home/ubuntu/server/.claude/worktrees/fanout-7/.fanout/server"
    assert f"rm -rf {snapshot};" in cmd
    assert f"env PYTHONPATH={snapshot}/scripts " in cmd
    assert f"{snapshot}/scripts/dev/fanout.py review --batch 7 " in cmd
    # The batch's tree and branch are the work being resumed.
    assert "worktree add" not in cmd and "branch -D" not in cmd
    assert "worktree remove" not in cmd


def test_the_resume_snapshot_holds_every_tree_the_pipeline_reads_at_start():
    read_at_start = [
        HOOKS_DIR.as_posix(),
        RULES_DIR.as_posix(),
        *(top.as_posix() for top, _ in HELD_TREES),
        ".claude/settings.json",
        "CLAUDE.md",
        SYSTEM_PROMPT_FILE,
    ]
    for wanted in read_at_start:
        assert any(
            wanted == held or wanted.startswith(f"{held}/")
            for held in RESUME_SNAPSHOT_PATHS
        ), wanted


def untracked(paths, trees, tracked: set[str]) -> list[str]:
    """Each snapshot path no tracked file sits at or under, and each held tree with no match."""
    missing = [
        p for p in paths if not any(f == p or f.startswith(f"{p}/") for f in tracked)
    ]
    for top, pattern in trees:
        under = [f for f in tracked if f.startswith(f"{top.as_posix()}/")]
        if not any(fnmatch(f.rsplit("/", 1)[-1], pattern) for f in under):
            missing.append(f"{top.as_posix()}/**/{pattern}")
    return missing


def test_every_snapshot_path_and_held_tree_names_a_tracked_file():
    paths = sorted({*SNAPSHOT_PATHS, *RESUME_SNAPSHOT_PATHS})
    assert untracked(paths, HELD_TREES, tracked_files(REPO)) == []


@pytest.mark.parametrize(
    "paths, trees",
    [
        (["inventory"], []),
        ([], [(Path("inventory"), "*")]),
        ([], [(Path("hosts"), "*.py")]),
    ],
)
def test_a_path_or_tree_no_tracked_file_matches_is_flagged(paths, trees):
    assert untracked(paths, trees, {"hosts/vars.yml", "scripts/x.py"})


def test_a_path_and_tree_a_tracked_file_matches_is_clean():
    tracked = {"hosts/vars.yml", "hosts/daniel-box/vars.yml", "scripts/x.py"}
    assert (
        untracked(["hosts", "scripts/x.py"], [(Path("hosts"), "vars.yml")], tracked)
        == []
    )
