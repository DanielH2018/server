"""A coordinator project with no live session is listed, and paged once while a batch waits.

Run: uv run pytest scripts/dev/tests/fanout/test_fanout_coordinators.py
"""

import json
import subprocess

from dev.fanout_lib.coordinators import (
    Project,
    alert_name,
    dead_lines,
    page_orphaned,
    projects,
    report,
)
from dev.fanout_lib.manifest import Batch, Manifest
from dev.fanout_lib.notify import NO_COORDINATOR, page_command
from lib.worktrees import COORDINATOR_LOCK, Worktree

BRANCH = "claude/backups-1a2b3c"
PATH = "/repo/.claude/worktrees/backups-1a2b3c"
PARKED = Batch(
    "41", "daniel-box", "/w/41", "worktree-fanout-41", "fanout-41", [41], "t"
)
HELD = Batch(
    "42", "daniel-server", "/w/42", "worktree-fanout-42", "fanout-42", [42], "t"
)
RUNNING = Batch(
    "43", "daniel-box", "/w/43", "worktree-fanout-43", "fanout-43", [43], "t"
)
OTHERS = Batch(
    "44", "daniel-box", "/w/44", "worktree-fanout-44", "fanout-44", [44], "t"
)
ORPHAN = Project(
    "Backups", BRANCH, PATH, live=False, waiting=(("41", "daniel-box", "parked"),)
)


def _tree(path: str, branch: str, reason: str) -> Worktree:
    return Worktree(path, "abc", branch, locked=bool(reason), lock_reason=reason)


def _runs() -> list[Manifest]:
    return [
        Manifest("r1", BRANCH, [PARKED, HELD, RUNNING]),
        Manifest("r2", "claude/another-project", [OTHERS]),
    ]


def _holds(batches):
    assert OTHERS not in batches, "another coordinator's batch was read"
    return {
        ("daniel-box", "41"): ("parked", "parked: DEPLOY_HOLD is set"),
        ("daniel-server", "42"): ("needs-input", "needs input: the head moved"),
    }


def test_a_project_with_no_live_session_names_its_branch_path_and_waiting_batches():
    trees = [_tree(PATH, BRANCH, f"{COORDINATOR_LOCK}Backups")]

    found = projects(trees, live=lambda tree: False, runs=_runs, holds=_holds)

    assert found == [
        Project(
            "Backups",
            BRANCH,
            PATH,
            live=False,
            waiting=(
                ("41", "daniel-box", "parked"),
                ("42", "daniel-server", "needs-input"),
            ),
        )
    ]
    assert dead_lines(found)[0] == (
        f'  ✗ project "Backups" has no live session: {BRANCH} at {PATH}; '
        "2 waiting: 41 on daniel-box parked, 42 on daniel-server needs-input"
    )


def test_a_live_project_is_listed_live_and_no_batch_is_read():
    def unread(*_):
        raise AssertionError("a live project's batches were read")

    trees = [_tree(PATH, BRANCH, f"{COORDINATOR_LOCK}Backups")]

    found = projects(trees, live=lambda tree: True, runs=unread, holds=unread)

    assert found == [Project("Backups", BRANCH, PATH, live=True)]
    assert dead_lines(found) == []


def test_a_worktree_with_no_project_lock_is_not_a_project():
    # An ended project is unlocked, and a session's or a batch's lock names no project.
    trees = [
        _tree("/repo", "master", ""),
        _tree(PATH, BRANCH, ""),
        _tree("/w/s", "claude/s", "claude session s (pid 1 start 2)"),
        _tree("/w/41", "worktree-fanout-41", "fanout-41"),
    ]

    assert projects(trees, live=lambda tree: False, runs=_runs, holds=_holds) == []


class Starts:
    def __init__(self, returncode: int = 0):
        self.calls: list[list[str]] = []
        self.returncode = returncode

    def __call__(self, argv, **_):
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, self.returncode, "", "")


def test_an_orphaned_project_pages_once_on_its_second_pass(tmp_path):
    # fact: hosts/components/claude-code/CLAUDE.md#Autonomous-role contract (`claude-fanout-server-lander`, a landing with no session behind it)
    record, start = tmp_path / "state" / "pages.json", Starts()

    page_orphaned([ORPHAN], record, lambda line: None, start)
    assert start.calls == [], "one pass with no process in the tree paged"

    page_orphaned([ORPHAN], record, lambda line: None, start)
    page_orphaned([ORPHAN], record, lambda line: None, start)

    unit = "fanout-coordinator-backups-1a2b3c"
    assert start.calls == [page_command(unit, NO_COORDINATOR)]
    assert json.loads(record.read_text()) == {PATH: True}


def test_a_pass_whose_read_fails_logs_a_line_and_does_not_raise(tmp_path):
    def unreadable() -> list[Project]:
        raise subprocess.CalledProcessError(128, ["git", "worktree", "list"])

    lines: list[str] = []

    report(tmp_path, lines.append, read=unreadable)

    assert len(lines) == 1
    assert lines[0].startswith("coordinator projects not checked: ")


def test_a_project_with_nothing_waiting_or_a_live_session_never_pages(tmp_path):
    record, start = tmp_path / "pages.json", Starts()
    quiet = [
        Project("Idle", "claude/idle", "/w/idle", live=False),
        Project("Backups", BRANCH, PATH, live=True),
    ]

    for _ in range(3):
        page_orphaned(quiet, record, lambda line: None, start)

    assert start.calls == []
    assert not record.exists()


def test_a_project_that_recovers_and_is_orphaned_again_pages_again(tmp_path):
    record, start = tmp_path / "pages.json", Starts()

    def passes(*found: list[Project]) -> None:
        for one in found:
            page_orphaned(one, record, lambda line: None, start)

    passes([ORPHAN], [ORPHAN])
    passes([Project("Backups", BRANCH, PATH, live=True)])
    passes([ORPHAN], [ORPHAN])

    assert len(start.calls) == 2


def test_a_page_that_fails_to_start_is_tried_on_the_next_pass(tmp_path):
    record, refused = tmp_path / "pages.json", Starts(returncode=1)
    page_orphaned([ORPHAN], record, lambda line: None, refused)
    page_orphaned([ORPHAN], record, lambda line: None, refused)

    start = Starts()
    page_orphaned([ORPHAN], record, lambda line: None, start)

    assert len(refused.calls) == 1
    assert len(start.calls) == 1


def test_a_dry_run_starts_nothing_and_writes_nothing(tmp_path):
    record, start, lines = tmp_path / "pages.json", Starts(), []
    record.write_text(json.dumps({PATH: False}))

    page_orphaned([ORPHAN], record, lines.append, start, dry_run=True)

    assert start.calls == []
    assert json.loads(record.read_text()) == {PATH: False}
    assert lines[0].endswith("would page (dry run)")


def test_the_alert_name_keeps_to_the_characters_the_polkit_rule_admits():
    odd = Project("T", "b", "/w/claude+fix  the--thing", live=False)

    assert alert_name(odd) == "fanout-coordinator-claude-fix-the-thing"
