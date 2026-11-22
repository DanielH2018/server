"""The unit `fanout.py place resume` starts: what its one command runs, and from where.

The command's checks and refusals are in `test_fanout_abandon.py`, and its lock and cap in
`test_fanout_launch_gates.py`.

Run: uv run pytest scripts/dev/tests/test_fanout_resume.py
"""

from dev.fanout_lib.launch import SYSTEM_PROMPT_FILE
from dev.fanout_lib.resume import RESUME_SNAPSHOT_PATHS, resume_command
from dev.fanout_lib.review.held_hooks import HELD_TREES, HOOKS_DIR, RULES_DIR
from _fanout_fakes import as_operator


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
