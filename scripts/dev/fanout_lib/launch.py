"""Create the worktree, write the brief, start the transient service — spec §3."""

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
    return (
        f"git -C {REPO} fetch origin && "
        f"git -C {REPO} worktree add -b {branch_name(batch)} {worktree_path(batch)} origin/master"
    )


def remove_worktree_command(batch: str) -> str:
    return (
        f"git -C {REPO} worktree remove --force {worktree_path(batch)}; "
        f"git -C {REPO} branch -D {branch_name(batch)}"
    )


def write_brief_command(batch: str) -> str:
    wt = worktree_path(batch)
    return f"mkdir -p {wt}/.fanout && cat > {wt}/.fanout/brief.md"


def launch_command(batch: str) -> str:
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


def _check(proc, what: str) -> None:
    if proc.returncode != 0:
        raise LaunchError(f"{what} failed ({proc.returncode}): {proc.stderr.strip()}")


def launch(
    tools: Tools, host: str, batch: str, brief_text: str, issues: list[int]
) -> Batch:
    """Create the worktree, write the brief over stdin, then start the agent unit.

    Args:
        tools: the injectable process boundary.
        host: the host to launch on.
        batch: the batch id (issue numbers joined by `-`).
        brief_text: the brief to write to `.fanout/brief.md` in the new worktree.
        issues: the issue numbers in this batch, carried into the returned `Batch`.

    Returns:
        The launched batch's record, for the run manifest.

    Raises:
        LaunchError: the worktree-add, brief-write or systemd-run step failed. A failed
            worktree-add removes the half-made tree and its branch before raising; the
            brief-write and systemd-run steps leave the worktree in place for inspection.
    """
    proc = tools.run(host, create_worktree_command(batch), LAUNCH_TIMEOUT_S, None)
    if proc.returncode != 0:
        tools.run(host, remove_worktree_command(batch), LAUNCH_TIMEOUT_S, None)
        raise LaunchError(
            f"worktree add failed ({proc.returncode}): {proc.stderr.strip()}"
        )
    _check(
        tools.run(host, write_brief_command(batch), LAUNCH_TIMEOUT_S, brief_text),
        "brief write",
    )
    _check(
        tools.run(host, launch_command(batch), LAUNCH_TIMEOUT_S, None), "systemd-run"
    )
    return Batch(
        batch,
        host,
        worktree_path(batch),
        branch_name(batch),
        unit_name(batch),
        list(issues),
        datetime.now(UTC).isoformat(),
    )
