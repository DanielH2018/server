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
from fanout_lib.target import SERVER_TARGET, Target
from fanout_lib.transport import REPO, Tools

LAUNCH_TIMEOUT_S = 120.0
# The host whose GitOps tick pulls the primary checkout itself. Not imported from
# `brief.LANDS`, which is the host that LANDS a PR: the same host today, a different fact, and
# `brief` is already imported by `transport`, which `launch` imports.
TICK_HOST = "daniel-box"
# The early-stop paragraph from Anthropic's Opus 5.5 guide (*Unattended agentic runs*),
# adapted: a text-only end of turn is a progress report, and the brief's completion condition
# is what ends the run. A path relative to the unit's WorkingDirectory, the
# worktree, which `worktree add` checks out from origin/master, so the file is always there.
# It goes through this headless launch only; an interactive session never reads it.
SYSTEM_PROMPT_FILE = "scripts/dev/fanout_lib/headless_system_prompt.md"
CLAUDE_ARGS_PREFIX = (
    "claude -p --model opus --permission-mode auto --output-format json"
    " --max-budget-usd {budget} --append-system-prompt-file {prompt}"
)
# A runaway bound, not a tight one. Over 84 fan-out sessions the largest read 64M cached
# tokens and wrote 152k output tokens: about $18 at Opus 5.5's list prices ($0.20 per
# million cache reads, $20 per million output, $8 per million 1h cache writes).
# `renovate_agent.py` bounds its own headless session the same way. A spent budget ends the
# session with `is_error: true` and `terminal_reason: budget_exhausted`, which `status`
# reports as `failed`.
BUDGET_USD = 40
CLAUDE_ARGS = CLAUDE_ARGS_PREFIX.format(budget=BUDGET_USD, prompt=SYSTEM_PROMPT_FILE)
# DECIDED: `RuntimeMaxSec=` here, where `claude-rc-restart.service.j2` rejects it for
# claude-rc.service. systemd records its expiry as a failure (`Result=timeout`); for a
# long-lived service host that is a false alarm, and for a batch that ran out of time it is
# the verdict `status` should print. Five hours covers the longest fan-out session measured
# (286 minutes; the next longest was 104) and bounds one that waits on something forever.
RUNTIME_MAX_S = 5 * 3600
# The user manager's PATH lacks ~/.local/bin (claude, uv) and repo hooks need uv. The fnm
# default alias is where `node` lives: the dotfiles repo's `bin/gate` runs `node --test`, and an
# interactive shell finds node only through fnm's per-shell directory, which a unit never gets.
PATH = (
    "/home/ubuntu/.local/bin:/home/ubuntu/.local/share/fnm/aliases/default/bin:"
    "/usr/local/bin:/usr/bin:/bin"
)


class LaunchError(Exception):
    """A step of `launch_command`'s chain failed or timed out; str() carries the reason."""


def worktree_path(batch: str, target: Target = SERVER_TARGET) -> str:
    return f"{target.checkout}/.claude/worktrees/fanout-{batch}"


def branch_name(batch: str) -> str:
    return f"worktree-fanout-{batch}"


def unit_name(batch: str, target: Target = SERVER_TARGET) -> str:
    return f"{target.unit_prefix}-{batch}"


def _step(command: str, name: str) -> str:
    """Wrap one step of the launch chain so its own failure exits with a named sentinel.

    `command`'s own stderr (git's "fatal:", bash's own "Permission denied" on a failed
    redirect, systemd-run's "Failed to start...") isn't reliable evidence of which step
    ran — a `cat > path` failure never prints "cat:", since bash reports the redirect
    error itself rather than running `cat` at all. Echoing `fanout-step: <name>` to stderr
    right before exiting gives `_attribute_failure` something exact to read instead.
    """
    return f'{command} || {{ echo "fanout-step: {name}" >&2; exit 1; }}'


def exists_check_command(batch: str, target: Target = SERVER_TARGET) -> str:
    """Refuse before `fetch` when this batch's worktree or branch is already there.

    A relaunch of a failed batch would reach `worktree add`, which fails precisely
    because the tree and branch exist — and `worktree add` is a cleanup step, so the
    cleanup would then force-remove that tree and delete its branch. The failed agent's
    work went with it, with nothing in the output saying so. Checking first turns that
    into a refusal: `exists` is deliberately NOT in `_CLEANUP_STEPS`, so nothing is
    touched.
    """
    return _step(
        f"test ! -e {worktree_path(batch, target)} && "
        f"! git -C {target.checkout} show-ref --verify --quiet "
        f"refs/heads/{branch_name(batch)}",
        "exists",
    )


def fast_forward_primary_command(host: str) -> str:
    """Bring `host`'s primary checkout up to `origin/master`, or "" where a tick already does.

    Issue #2675: `.claude/settings.json` names every hook by an absolute path into the
    PRIMARY checkout, not into the session's worktree. A worktree cut from a fresher
    `origin/master` than the primary checkout therefore registers hook scripts the primary
    checkout does not have yet, `/bin/sh` exits 127, Claude Code treats that as a
    non-blocking hook error, and the tool call runs with the guard skipped. About 2,100 Bash
    calls ran that way on daniel-server across two windows in September 2026, each window
    opened by a commit adding a hook script and closed when that checkout next pulled.

    DECIDED: nothing on `TICK_HOST`, where the GitOps tick pulls every 10 minutes. The window
    there is bounded by the tick, and the tick takes the git-tree lock for its
    own `--ff-only` merge (`deploy_locks.TREE_LOCK`) because moving HEAD under an in-flight
    deploy ships a different SHA than the one the health gate cleared. A launch cannot hold
    that lock: a deploy holds it for up to 20 minutes, well past `LAUNCH_TIMEOUT_S`, so taking
    it would turn a bounded stale-hook window into a failed launch. The defect is a host with
    no tick, which is the only host this fast-forwards.

    Gated on HEAD being `master`: `merge --ff-only origin/master` on a checkout parked on
    another branch would take master's commits onto THAT branch. A refusal here — detached
    HEAD, a local commit, a diverged branch, a dirty tree `--ff-only` cannot cross — refuses
    the launch, which is the right answer rather than a fallback: the host's hook state is
    then unknown, and that is exactly when a batch must not be placed on it.
    """
    if host == TICK_HOST:
        return ""
    return _step(
        f"git -C {REPO} symbolic-ref --quiet --short HEAD | grep -qx master && "
        f"git -C {REPO} merge --ff-only origin/master",
        "primary ff",
    )


def exclude_fanout_command(target: Target) -> str:
    """Make git ignore `.fanout/` in `target`'s worktrees, or "" where the repo already does.

    The brief, the report and the stderr log all live in the worktree's `.fanout/`. This repo's
    `.gitignore` denies every root path, but the dotfiles `.gitignore` does not name it. There
    an agent's `git add -A` commits its own brief, and `clean` reads the untracked files as a
    dirty tree and keeps it forever. The line goes in the common `info/exclude`, which every
    linked worktree of the checkout reads, and is appended only when absent.
    """
    if target.is_server:
        return ""
    exclude = f"{target.checkout}/.git/info/exclude"
    return _step(
        f"mkdir -p {target.checkout}/.git/info && "
        f"{{ grep -qxF .fanout/ {exclude} 2>/dev/null || echo .fanout/ >> {exclude}; }}",
        "exclude",
    )


def create_worktree_command(
    batch: str, host: str, target: Target = SERVER_TARGET
) -> str:
    # The lock keeps prune_worktrees.py off this tree: its `--reason` doesn't match the
    # `claude session ... (pid ... start ...)` shape prune_worktrees.session_is_alive
    # recognizes, so an unrecognized reason reads as alive and the tree survives every
    # prune until Task 10's `clean` unlocks it. Without this a merged, clean, unlocked
    # tree is removable the moment the PR lands — even while the unit is still running.
    # The primary fast-forward is this repo's alone. Its reason is the hooks
    # `.claude/settings.json` registers by absolute path into this checkout, and in the
    # dotfiles checkout `bin/land-sync` owns `main`.
    wt = worktree_path(batch, target)
    steps = [
        exists_check_command(batch, target),
        _step(f"git -C {target.checkout} fetch origin", "fetch"),
        fast_forward_primary_command(host) if target.is_server else "",
        exclude_fanout_command(target),
        _step(
            f"git -C {target.checkout} worktree add -b {branch_name(batch)} "
            f"{wt} {target.base}",
            "worktree add",
        ),
        _step(
            f"git -C {target.checkout} worktree lock "
            f"--reason {unit_name(batch, target)} {wt}",
            "worktree lock",
        ),
    ]
    return " && ".join(s for s in steps if s)


def remove_worktree_command(batch: str, target: Target = SERVER_TARGET) -> str:
    # `git worktree remove` refuses a locked tree, so unlock first. A bare `;` here is
    # correct: on a tree that was never locked (or never fully created) the unlock fails
    # harmlessly, and the `&&` that follows still decides whether the branch dies —
    # `worktree add -b` fails precisely when the branch already exists, so a `;` there
    # would force-delete a branch this launch did not create whenever the add failed for
    # that reason. Chaining remove and branch -D on success means the branch survives
    # when no tree was created, and goes with the tree when the add did half-create it.
    wt, repo = worktree_path(batch, target), target.checkout
    return (
        f"git -C {repo} worktree unlock {wt}; "
        f"git -C {repo} worktree remove --force {wt} && "
        f"git -C {repo} branch -D {branch_name(batch)}"
    )


def write_brief_command(batch: str, target: Target = SERVER_TARGET) -> str:
    wt = worktree_path(batch, target)
    return _step(f"mkdir -p {wt}/.fanout && cat > {wt}/.fanout/brief.md", "brief write")


def claude_args(target: Target = SERVER_TARGET) -> str:
    """The `claude -p` command line a batch in `target` runs.

    The system prompt file is relative to the worktree for this repo, whose worktree always
    carries it. Another repo's worktree does not, so there it is read from this repo's primary
    checkout on the same host, and a missing file would end the session before its first turn.
    """
    if target.is_server:
        return CLAUDE_ARGS
    return CLAUDE_ARGS_PREFIX.format(
        budget=BUDGET_USD, prompt=f"{REPO}/{SYSTEM_PROMPT_FILE}"
    )


def systemd_run_command(batch: str, target: Target = SERVER_TARGET) -> str:
    wt = worktree_path(batch, target)
    return _step(
        (
            f"systemd-run --user --unit {unit_name(batch, target)} "
            f"-p WorkingDirectory={wt} "
            f"-p StandardInput=file:{wt}/.fanout/brief.md "
            f"-p StandardOutput=file:{wt}/.fanout/report.json "
            f"-p StandardError=file:{wt}/.fanout/stderr.log "
            f"-p Environment=PATH={PATH} -p Environment=HOME=/home/ubuntu "
            f"-p RuntimeMaxSec={RUNTIME_MAX_S} "
            f"{claude_args(target)}"
        ),
        "systemd-run",
    )


def prepare_command(batch: str, host: str, target: Target = SERVER_TARGET) -> str:
    """Worktree add+lock and the brief write, without starting the agent."""
    return " && ".join(
        [
            create_worktree_command(batch, host, target),
            write_brief_command(batch, target),
        ]
    )


def launch_command(batch: str, host: str) -> str:
    """The one call a batch launch runs: worktree add+lock, brief write, systemd-run.

    The brief text is this command's own stdin, consumed by the `cat` in the middle of the
    chain. Each step is wrapped by `_step` to exit on its own failure, so a failure
    anywhere stops the rest — a failed worktree add never reaches `cat` or `systemd-run`,
    and a failed brief write never reaches `systemd-run`.
    """
    return " && ".join(
        [
            create_worktree_command(batch, host),
            write_brief_command(batch),
            systemd_run_command(batch),
        ]
    )


_STEP_SENTINEL_RE = re.compile(r"^fanout-step: (.+)$", re.MULTILINE)

# Cleanup removes the worktree and its branch, so it only runs for a step that could have
# left one half-made: `worktree add`/`worktree lock` do; `fetch` and `primary ff` failures
# precede both and created nothing (cleanup there would fail its own `worktree remove` with a
# confusing "not a working tree"); `brief write`/`systemd-run` come after the tree already exists and
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


def _cleanup_worktree(
    tools: Tools, host: str, batch: str, target: Target = SERVER_TARGET
) -> str | None:
    """Remove a half-made worktree; return a message suffix on failure, else None."""
    try:
        cleanup = _run(
            tools,
            host,
            remove_worktree_command(batch, target),
            None,
            "worktree cleanup",
        )
    except LaunchError as exc:
        return f"; {exc}"
    if cleanup.returncode != 0:
        return f"; cleanup failed ({cleanup.returncode}): {cleanup.stderr.strip()}"
    return None


def _claim(tools: Tools, batch: str, issues: list[int], target: Target) -> str | None:
    """Claim `issues` under the batch's own branch in `target`'s register; None when taken.

    Another repo's batch is claimed here rather than by the orchestrator, and only once its
    tree exists. `findings.py` judges a claim in that register against the repo's own
    checkout, where the orchestrator's branch does not exist, so a claim under it is stale
    the moment it is written. A claim under the batch's branch is live only while that branch
    has a locked tree, and `launch` is the one step that knows when the tree exists and the
    agent has not yet started.

    On a refusal, every issue of the batch is released again. `release` refuses any claim but
    its own, so an issue another worktree holds keeps its claim.

    Returns:
        None when every issue was claimed, else the reason to report.
    """
    numbers = [str(n) for n in issues]
    common = ["--worktree", branch_name(batch), "--repo", target.repo]
    try:
        claimed = tools.findings(["claim", *numbers, *common])
    except subprocess.TimeoutExpired:
        claimed = None
    if claimed is not None and claimed.returncode == 0:
        return None
    if claimed is None:
        reason = "claim timed out"
    else:
        detail = (claimed.stderr or claimed.stdout).strip()
        reason = f"claim refused ({claimed.returncode}): {detail}"
    try:
        tools.findings(
            ["release", *numbers, *common, "--reason", "fan-out launch refused"]
        )
    except subprocess.TimeoutExpired:
        reason += "; release timed out"
    return reason


def _launch_elsewhere(
    tools: Tools,
    host: str,
    batch: str,
    brief_text: str,
    issues: list[int],
    target: Target,
) -> None:
    """Launch a batch in another repo: prepare the tree, claim, then start the agent.

    Three calls rather than `launch_command`'s one, because the claim has to fall between the
    tree and the agent. They cost no ssh connection, since `cmd_launch` pins every such batch
    to the host it runs on.

    Raises:
        LaunchError: as `launch` documents, plus a refused claim, which removes the tree.
    """
    try:
        proc = _run(
            tools, host, prepare_command(batch, host, target), brief_text, "launch"
        )
    except LaunchError as exc:
        message = str(exc) + (_cleanup_worktree(tools, host, batch, target) or "")
        raise LaunchError(message) from None
    if proc.returncode != 0:
        _raise_failure(host, batch, target, proc, tools)
    refused = _claim(tools, batch, issues, target)
    if refused:
        cleanup = _cleanup_worktree(tools, host, batch, target) or ""
        raise LaunchError(f"claim: {refused}{cleanup}")
    proc = _run(tools, host, systemd_run_command(batch, target), None, "systemd-run")
    if proc.returncode != 0:
        _raise_failure(host, batch, target, proc, tools)


def _raise_failure(
    host: str,
    batch: str,
    target: Target,
    proc: subprocess.CompletedProcess,
    tools: Tools,
) -> None:
    """Raise the `LaunchError` for a failed launch call, cleaning up where `launch` says to."""
    step = _attribute_failure(proc.stderr)
    if step == _EXISTS_STEP:
        raise LaunchError(
            f"batch {batch} already has {worktree_path(batch, target)} or branch "
            f"{branch_name(batch)} on {host} — run `clean <run-id>` first, or remove "
            "the tree by hand if you are abandoning its work; relaunching over it "
            "would delete that branch"
        )
    message = (
        f"{step or 'launch command'} failed ({proc.returncode}): {proc.stderr.strip()}"
    )
    if step in _CLEANUP_STEPS:
        message += _cleanup_worktree(tools, host, batch, target) or ""
    raise LaunchError(message)


def launch(
    tools: Tools,
    host: str,
    batch: str,
    brief_text: str,
    issues: list[int],
    target: Target = SERVER_TARGET,
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
        target: the repo the batch works. Another repo's batch is claimed here, under its
            own branch, between the tree and the agent; see `_claim`. A refused claim
            removes the tree and raises.

    Returns:
        The launched batch's record, for the run manifest.

    Raises:
        LaunchError: the launch call failed or timed out. An `exists` failure means this
            batch's worktree or branch is already on the host — usually a relaunch of a
            batch that failed — and the message says to `clean` it first; nothing is
            removed. A `worktree add`/`worktree lock` failure (or a timeout, which is a
            hung git step in practice — see the `DECIDED:` note above the cleanup check)
            removes the half-made tree and its branch before raising, folding a cleanup
            failure into the same message. A `fetch`, `primary ff`, `brief write` or
            `systemd-run` failure, or one this can't attribute, leaves the worktree as it
            found it instead.
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
    if not target.is_server:
        _launch_elsewhere(tools, host, batch, brief_text, issues, target)
    else:
        try:
            proc = _run(tools, host, launch_command(batch, host), brief_text, "launch")
        except LaunchError as exc:
            message = str(exc) + (_cleanup_worktree(tools, host, batch) or "")
            raise LaunchError(message) from None
        if proc.returncode != 0:
            _raise_failure(host, batch, target, proc, tools)
    return Batch(
        batch,
        host,
        worktree_path(batch, target),
        branch_name(batch),
        unit_name(batch, target),
        list(issues),
        datetime.now(UTC).isoformat(),
        repo=target.repo,
    )
