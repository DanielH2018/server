"""Coordinator projects, and the page for one whose sub-coordinator session is gone.

A sub-coordinator locks its worktree with `lib.worktrees.COORDINATOR_LOCK` and its project's
title, and launches its batches under that worktree's branch. When its session dies, the
lock, the runs and the claims all stand, so a replacement session rebuilds the project. But
its `parked` batches and the PRs the server lander holds wait on a session that is gone, and
the root coordinator can start the replacement only on a turn the operator types.

`projects` is the one read of which projects exist and which have no live session. Two
callers print or page from it: `session-health.py --sessions`, and each pass of the server
lander (`autoland.main`), which is the timer that already reads fan-out state. No session
polls for it.
"""

import json
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from dev.fanout_lib.manifest import Batch, Manifest, all_runs
from dev.fanout_lib.notify import NO_COORDINATOR, START_TIMEOUT_S, page_command
from dev.fanout_lib.status import landing_holds
from dev.fanout_lib.transport import Tools
from lib.git import git
from lib.repo_paths import REPO
from lib.worktrees import (
    Worktree,
    coordinator_project,
    has_live_session,
    parse_worktree_list,
    primary_checkout,
)

# The lander's record of the projects it has seen orphaned, in its state directory.
PAGES_FILE = "coordinator-pages.json"


@dataclass(frozen=True)
class Project:
    """One coordinator project: a worktree locked for it, and whether a session holds it.

    Attributes:
        title: the project's milestone title, from the lock reason.
        branch: the worktree's branch, which its runs and claims are under; "" when detached.
        path: the worktree.
        live: whether a session works in the worktree at this moment.
        waiting: (batch, host, state) for each batch whose landing needs its coordinator,
            `parked` or `needs-input`. Read only for a project that is not live.
    """

    title: str
    branch: str
    path: str
    live: bool
    waiting: tuple[tuple[str, str, str], ...] = ()


def _worktrees() -> list[Worktree]:
    repo = primary_checkout() or str(REPO)
    return parse_worktree_list(git("worktree", "list", "--porcelain", cwd=repo).stdout)


def _holds(batches: Sequence[Batch]) -> dict[tuple[str, str], tuple[str, str]]:
    return landing_holds(batches, Tools())


def projects(
    trees: Sequence[Worktree] | None = None,
    live: Callable[[Worktree], bool] = has_live_session,
    runs: Callable[[], list[Manifest]] = all_runs,
    holds: Callable[[Sequence[Batch]], dict[tuple[str, str], tuple[str, str]]] = _holds,
) -> list[Project]:
    """Every coordinator project on this host, with the batches a dead one leaves waiting.

    A project is a worktree whose lock names one. A project the operator ended is unlocked
    (`.claude/skills/coordinate/SKILL.md`, *Ending a project*), so it is not listed. A
    waiting batch is one `status.landing_holds` returns: `parked`, or a PR the server
    lander will not land. That read asks each batch's host and never GitHub.

    Args:
        trees: the repo's worktrees; read from git when None.
        live: whether a session holds a locked worktree.
        runs: every run manifest of this user.
        holds: `status.landing_holds` over a list of standing batches.

    Raises:
        subprocess.CalledProcessError: git could not list the worktrees.
    """
    found = []
    manifests = None
    for tree in _worktrees() if trees is None else trees:
        title = coordinator_project(tree)
        if not title:
            continue
        branch = tree.branch or ""
        if live(tree):
            found.append(Project(title, branch, tree.path, True))
            continue
        if manifests is None:
            manifests = runs()
        standing = [
            b
            for run in manifests
            if branch and run.orchestrator_branch == branch
            for b in run.batches
            if not b.removed_at
        ]
        held = holds(standing) if standing else {}
        waiting = tuple(
            (b.batch, b.host, held[(b.host, b.batch)][0])
            for b in standing
            if (b.host, b.batch) in held
        )
        found.append(Project(title, branch, tree.path, False, waiting))
    return found


def _waiting_text(p: Project) -> str:
    if not p.waiting:
        return "no batch waits on it"
    batches = ", ".join(
        f"{batch} on {host} {state}" for batch, host, state in p.waiting
    )
    return f"{len(p.waiting)} waiting: {batches}"


def dead_lines(found: Sequence[Project]) -> list[str]:
    """One line per project with no live session, then the step that restarts them; else []."""
    lines = [
        f'  ✗ project "{p.title}" has no live session: {p.branch or "detached"} at '
        f"{p.path}; {_waiting_text(p)}"
        for p in found
        if not p.live
    ]
    if lines:
        lines.append(
            "  → restart each in its own worktree with its title "
            "(`coordinate` skill, *Root*); the operator has to ask for the start"
        )
    return lines


def alert_name(p: Project) -> str:
    """The name the page carries for `p`, in the characters the polkit rule admits."""
    slug = re.sub(r"[^A-Za-z0-9_.]+", "-", Path(p.path).name).strip("-")
    return f"fanout-coordinator-{slug}" if slug else "fanout-coordinator"


def _read_pages(path: Path) -> dict[str, bool]:
    try:
        data = json.loads(path.read_text())
    except OSError, ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): v is True for k, v in data.items()}


# DECIDED: a project pages on the second pass in a row that reads it orphaned, and once.
# A desktop session's process restarts mid-session (measured on daniel-box, 2026-11-20: a
# working session's process was two minutes old), so one pass with no process in the tree is
# not a death. The record drops a project as soon as a pass reads it live or with nothing
# waiting, so the next orphaning pages again.
def page_orphaned(
    found: Sequence[Project],
    record: Path,
    out: Callable[[str], None] = print,
    start: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    dry_run: bool = False,
) -> None:
    """Page the operator for each project with no live session and a batch waiting on it.

    Starts `fanout-alert@<alert_name>:no-coordinator.service`, the unit a stopped batch
    pages through. A page that fails to start is tried again on the next pass.

    Args:
        found: `projects()`.
        record: the file that holds which orphaned projects were seen and paged.
        out: takes one line per project whose record changed.
        start: runs the `systemctl start`.
        dry_run: print what would happen, start nothing and write nothing.

    Raises:
        OSError: the record could not be written.
    """
    before = _read_pages(record)
    after: dict[str, bool] = {}
    for p in found:
        if p.live or not p.waiting:
            continue
        label = (
            f'project "{p.title}" ({p.branch}) has no live session, {_waiting_text(p)}'
        )
        if p.path not in before:
            after[p.path] = False
            out(f"{label}: first pass, it pages on the next")
        elif before[p.path]:
            after[p.path] = True
        elif dry_run:
            after[p.path] = False
            out(f"{label}: would page (dry run)")
        else:
            command = page_command(alert_name(p), NO_COORDINATOR)
            done = start(
                command,
                capture_output=True,
                text=True,
                timeout=START_TIMEOUT_S,
                check=False,
            )
            after[p.path] = done.returncode == 0
            how = "paged" if after[p.path] else "page FAILED to start"
            out(f"{label}: {how} through {command[-1]} {done.stderr.strip()}".rstrip())
    if dry_run or after == before:
        return
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps(after, sort_keys=True) + "\n")


def report(
    state: Path,
    out: Callable[[str], None] = print,
    dry_run: bool = False,
    read: Callable[[], list[Project]] = projects,
) -> None:
    """One lander pass's coordinator check. A failed read or write is a line, never a raise."""
    try:
        page_orphaned(read(), state / PAGES_FILE, out, dry_run=dry_run)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        out(f"coordinator projects not checked: {str(exc).strip()[-300:]}")
