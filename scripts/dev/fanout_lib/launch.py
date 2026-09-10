"""Create the worktree, write the brief, start the transient service — spec §3."""

import subprocess
from datetime import UTC, datetime

# Reach the sibling package: a directly-invoked script gets only its own directory on
# sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from fanout_lib.manifest import Batch
from fanout_lib.transport import REPO, Tools

LAUNCH_TIMEOUT_S = 120.0
CLAUDE_ARGS = "claude -p --model opus --permission-mode auto --output-format json"
# The user manager's PATH lacks ~/.local/bin (claude, uv) and repo hooks need uv.
PATH = "/home/ubuntu/.local/bin:/usr/local/bin:/usr/bin:/bin"


class LaunchError(Exception):
    """A worktree-add, brief-write or systemd-run step failed; str() carries the reason."""


def worktree_path(batch: str) -> str:
    return f"{REPO}/.claude/worktrees/fanout-{batch}"


def branch_name(batch: str) -> str:
    return f"worktree-fanout-{batch}"


def unit_name(batch: str) -> str:
    return f"fanout-{batch}"


def create_worktree_command(batch: str) -> str:
    # The lock keeps prune_worktrees.py off this tree: its `--reason` doesn't match the
    # `claude session ... (pid ... start ...)` shape prune_worktrees.session_is_alive
    # recognizes, so an unrecognized reason reads as alive and the tree survives every
    # prune until Task 10's `clean` unlocks it. Without this a merged, clean, unlocked
    # tree is removable the moment the PR lands — even while the unit is still running.
    return (
        f"git -C {REPO} fetch origin && "
        f"git -C {REPO} worktree add -b {branch_name(batch)} {worktree_path(batch)} origin/master && "
        f"git -C {REPO} worktree lock --reason {unit_name(batch)} {worktree_path(batch)}"
    )


def remove_worktree_command(batch: str) -> str:
    # `git worktree remove` refuses a locked tree, so unlock first. A bare `;` here is
    # correct: on a tree that was never locked (or never fully created) the unlock fails
    # harmlessly, and the `&&` that follows still decides whether the branch dies —
    # `worktree add -b` fails precisely when the branch already exists, so a `;` there
    # would force-delete a branch this launch did not create whenever the add failed for
    # that reason. Chaining remove and branch -D on success means the branch survives
    # when no tree was created, and goes with the tree when the add did half-create it.
    return (
        f"git -C {REPO} worktree unlock {worktree_path(batch)}; "
        f"git -C {REPO} worktree remove --force {worktree_path(batch)} && "
        f"git -C {REPO} branch -D {branch_name(batch)}"
    )


def write_brief_command(batch: str) -> str:
    wt = worktree_path(batch)
    return f"mkdir -p {wt}/.fanout && cat > {wt}/.fanout/brief.md"


def systemd_run_command(batch: str) -> str:
    wt = worktree_path(batch)
    return (
        f"systemd-run --user --unit {unit_name(batch)} "
        f"-p WorkingDirectory={wt} "
        f"-p StandardInput=file:{wt}/.fanout/brief.md "
        f"-p StandardOutput=file:{wt}/.fanout/report.json "
        f"-p StandardError=file:{wt}/.fanout/stderr.log "
        f"-p Environment=PATH={PATH} -p Environment=HOME=/home/ubuntu "
        f"{CLAUDE_ARGS}"
    )


def launch_command(batch: str) -> str:
    """The one call a batch launch runs: worktree add+lock, brief write, systemd-run.

    The brief text is this command's own stdin, consumed by the `cat` in the middle of
    the chain. `&&` between every step means a failure anywhere stops the rest — a failed
    worktree add never reaches `cat` or `systemd-run`, and a failed brief write never
    reaches `systemd-run`.
    """
    return " && ".join(
        [
            create_worktree_command(batch),
            write_brief_command(batch),
            systemd_run_command(batch),
        ]
    )


# git's own failures open with "fatal:"; a `cat` failure opens with "cat:"; systemd-run's
# failure carries this phrase. These are the only steps in `launch_command`'s chain that can
# fail, so a message matching none of them means the chain failed at a step none of the
# three markers cover.
_SYSTEMD_RUN_FAILURE_MARKER = "Failed to start transient service"


def _attribute_failure(stderr: str) -> str | None:
    """Name which step of `launch_command`'s chain produced this stderr, or None.

    The chain runs as one call, so stderr is the only evidence of which step failed.
    """
    if "fatal:" in stderr:
        return "worktree add"
    if "cat:" in stderr:
        return "brief write"
    if _SYSTEMD_RUN_FAILURE_MARKER in stderr:
        return "systemd-run"
    return None


def _run(
    tools: Tools, host: str, command: str, stdin: str | None, step: str
) -> subprocess.CompletedProcess:
    """Run `command` through `tools.run`, turning a timeout into a `LaunchError`."""
    try:
        return tools.run(host, command, LAUNCH_TIMEOUT_S, stdin)
    except subprocess.TimeoutExpired:
        raise LaunchError(f"{step} timed out after {LAUNCH_TIMEOUT_S}s") from None


def _cleanup_worktree(tools: Tools, host: str, batch: str) -> str | None:
    """Remove a half-made worktree; return a message suffix on failure, else None."""
    try:
        cleanup = _run(
            tools, host, remove_worktree_command(batch), None, "worktree cleanup"
        )
    except LaunchError as exc:
        return f"; {exc}"
    if cleanup.returncode != 0:
        return f"; cleanup failed ({cleanup.returncode}): {cleanup.stderr.strip()}"
    return None


def launch(
    tools: Tools, host: str, batch: str, brief_text: str, issues: list[int]
) -> Batch:
    """Create the worktree, write the brief over stdin, then start the agent unit.

    The worktree is locked with reason `fanout-<batch>` (`unit_name(batch)`) so a
    merged-worktree prune cannot remove it while the unit is still running; Task 10's
    `clean` is what unlocks it once the unit finishes. All three steps run as ONE call
    (`launch_command`), the brief arriving on its stdin, to keep a batch's launch to a
    single ssh connection.

    Args:
        tools: the injectable process boundary.
        host: the host to launch on.
        batch: the batch id (issue numbers joined by `-`).
        brief_text: the brief to write to `.fanout/brief.md` in the new worktree.
        issues: the issue numbers in this batch, carried into the returned `Batch`.

    Returns:
        The launched batch's record, for the run manifest.

    Raises:
        LaunchError: the launch call failed or timed out. A worktree-add failure (or a
            timeout, which is a hung `git fetch`/`worktree add` in practice — `systemd-run`
            starts the unit and returns before the ssh connection could plausibly still be
            open) removes the half-made tree and its branch before raising, folding a
            cleanup failure into the same message. A brief-write or systemd-run failure
            leaves the worktree in place for inspection instead.
    """
    try:
        proc = _run(tools, host, launch_command(batch), brief_text, "launch")
    except LaunchError as exc:
        message = str(exc) + (_cleanup_worktree(tools, host, batch) or "")
        raise LaunchError(message) from None
    if proc.returncode != 0:
        step = _attribute_failure(proc.stderr)
        message = f"{step or 'launch command'} failed ({proc.returncode}): {proc.stderr.strip()}"
        if step == "worktree add":
            message += _cleanup_worktree(tools, host, batch) or ""
        raise LaunchError(message)
    return Batch(
        batch,
        host,
        worktree_path(batch),
        branch_name(batch),
        unit_name(batch),
        list(issues),
        datetime.now(UTC).isoformat(),
    )
