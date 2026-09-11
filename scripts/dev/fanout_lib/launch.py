"""Create the worktree, write the brief, start the transient service — spec §3."""

import re
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
    """A step of `launch_command`'s chain failed or timed out; str() carries the reason."""


def worktree_path(batch: str) -> str:
    return f"{REPO}/.claude/worktrees/fanout-{batch}"


def branch_name(batch: str) -> str:
    return f"worktree-fanout-{batch}"


def unit_name(batch: str) -> str:
    return f"fanout-{batch}"


def _step(command: str, name: str) -> str:
    """Wrap one step of the launch chain so its own failure exits with a named sentinel.

    `command`'s own stderr (git's "fatal:", bash's own "Permission denied" on a failed
    redirect, systemd-run's "Failed to start...") isn't reliable evidence of which step
    ran — a `cat > path` failure never prints "cat:", since bash reports the redirect
    error itself rather than running `cat` at all. Echoing `fanout-step: <name>` to stderr
    right before exiting gives `_attribute_failure` something exact to read instead.
    """
    return f'{command} || {{ echo "fanout-step: {name}" >&2; exit 1; }}'


def exists_check_command(batch: str) -> str:
    """Refuse before `fetch` when this batch's worktree or branch is already there.

    A relaunch of a failed batch used to reach `worktree add`, which fails precisely
    because the tree and branch exist — and `worktree add` is a cleanup step, so the
    cleanup then force-removed that tree and deleted its branch. The failed agent's work
    went with it, with nothing in the output saying so. Checking first turns that into a
    refusal: `exists` is deliberately NOT in `_CLEANUP_STEPS`, so nothing is touched.
    """
    return _step(
        f"test ! -e {worktree_path(batch)} && "
        f"! git -C {REPO} show-ref --verify --quiet refs/heads/{branch_name(batch)}",
        "exists",
    )


def create_worktree_command(batch: str) -> str:
    # The lock keeps prune_worktrees.py off this tree: its `--reason` doesn't match the
    # `claude session ... (pid ... start ...)` shape prune_worktrees.session_is_alive
    # recognizes, so an unrecognized reason reads as alive and the tree survives every
    # prune until Task 10's `clean` unlocks it. Without this a merged, clean, unlocked
    # tree is removable the moment the PR lands — even while the unit is still running.
    return " && ".join(
        [
            exists_check_command(batch),
            _step(f"git -C {REPO} fetch origin", "fetch"),
            _step(
                f"git -C {REPO} worktree add -b {branch_name(batch)} "
                f"{worktree_path(batch)} origin/master",
                "worktree add",
            ),
            _step(
                f"git -C {REPO} worktree lock --reason {unit_name(batch)} "
                f"{worktree_path(batch)}",
                "worktree lock",
            ),
        ]
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
    return _step(f"mkdir -p {wt}/.fanout && cat > {wt}/.fanout/brief.md", "brief write")


def systemd_run_command(batch: str) -> str:
    wt = worktree_path(batch)
    return _step(
        (
            f"systemd-run --user --unit {unit_name(batch)} "
            f"-p WorkingDirectory={wt} "
            f"-p StandardInput=file:{wt}/.fanout/brief.md "
            f"-p StandardOutput=file:{wt}/.fanout/report.json "
            f"-p StandardError=file:{wt}/.fanout/stderr.log "
            f"-p Environment=PATH={PATH} -p Environment=HOME=/home/ubuntu "
            f"{CLAUDE_ARGS}"
        ),
        "systemd-run",
    )


def launch_command(batch: str) -> str:
    """The one call a batch launch runs: worktree add+lock, brief write, systemd-run.

    The brief text is this command's own stdin, consumed by the `cat` in the middle of the
    chain. Each step is wrapped by `_step` to exit on its own failure, so a failure
    anywhere stops the rest — a failed worktree add never reaches `cat` or `systemd-run`,
    and a failed brief write never reaches `systemd-run`.
    """
    return " && ".join(
        [
            create_worktree_command(batch),
            write_brief_command(batch),
            systemd_run_command(batch),
        ]
    )


_STEP_SENTINEL_RE = re.compile(r"^fanout-step: (.+)$", re.MULTILINE)

# Cleanup removes the worktree and its branch, so it only runs for a step that could have
# left one half-made: `worktree add`/`worktree lock` do; a `fetch` failure precedes both and
# created nothing (cleanup there would fail its own `worktree remove` with a confusing
# "not a working tree"); `brief write`/`systemd-run` come after the tree already exists and
# leave it in place for inspection instead. `exists` is the one that must never be here: it
# fails BECAUSE a tree is there, and that tree belongs to an earlier batch, not this launch.
_CLEANUP_STEPS = frozenset({"worktree add", "worktree lock"})

_EXISTS_STEP = "exists"


def _attribute_failure(stderr: str) -> str | None:
    """Name which step of `launch_command`'s chain produced this stderr, or None.

    The chain runs as one call, so the `fanout-step:` sentinel each step's `_step` wrapper
    echoes on failure is the only evidence of which step failed. Reads the LAST such line
    in case an earlier, successful step's own stderr (e.g. `git fetch`'s progress text)
    happens to contain the same words.
    """
    matches = _STEP_SENTINEL_RE.findall(stderr)
    return matches[-1] if matches else None


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
        LaunchError: the launch call failed or timed out. An `exists` failure means this
            batch's worktree or branch is already on the host — usually a relaunch of a
            batch that failed — and the message says to `clean` it first; nothing is
            removed. A `worktree add`/`worktree lock` failure (or a timeout, which is a
            hung git step in practice — see the `DECIDED:` note above the cleanup check)
            removes the half-made tree and its branch before raising, folding a cleanup
            failure into the same message. A `fetch`, `brief write` or `systemd-run`
            failure, or one this can't attribute, leaves the worktree as it found it
            instead.
    """
    # DECIDED: no exit or timeout from this call can happen after the unit is live.
    # `systemd-run` (without --wait/--pty/--scope) starts the transient unit and returns
    # immediately, so this call is still running only while an earlier step (fetch, worktree
    # add/lock, or the brief write) is — never after `systemd-run` has handed off. That's
    # what makes an unconditional cleanup safe on a timeout, and what makes `--scope`
    # forbidden here: it would tie the agent to this ssh connection, and a cleanup after
    # that would remove a worktree a live unit still needs.
    # `test_the_launch_command_folds_every_step_into_one_call_ending_in_systemd_run` asserts
    # `"--scope" not in cmd` as the guard.
    try:
        proc = _run(tools, host, launch_command(batch), brief_text, "launch")
    except LaunchError as exc:
        message = str(exc) + (_cleanup_worktree(tools, host, batch) or "")
        raise LaunchError(message) from None
    if proc.returncode != 0:
        step = _attribute_failure(proc.stderr)
        if step == _EXISTS_STEP:
            raise LaunchError(
                f"batch {batch} already has {worktree_path(batch)} or branch "
                f"{branch_name(batch)} on {host} — run `clean <run-id>` first, or remove "
                "the tree by hand if you are abandoning its work; relaunching over it "
                "would delete that branch"
            )
        message = f"{step or 'launch command'} failed ({proc.returncode}): {proc.stderr.strip()}"
        if step in _CLEANUP_STEPS:
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
