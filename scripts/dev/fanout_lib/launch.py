"""Create the worktree, write the brief, start the transient service — spec §3."""

import json
import re
import shlex
import subprocess
from datetime import UTC, datetime

# Reach the sibling package: a directly-invoked script gets only its own directory on
# sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from fanout_lib.manifest import Batch
from fanout_lib.target import SERVER_TARGET, Target, branch_name
from fanout_lib.transport import REPO, Tools

LAUNCH_TIMEOUT_S = 120.0
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


# The `fanout-stop` Stop hook, registered for another repo's batch through `--settings`.
# Claude Code loads project settings from the session's cwd, so a batch in this repo's
# worktree gets the hook from `.claude/settings.json`, and a batch in the dotfiles worktree,
# which has no such file, got no hook at all (#3363). Passing it here for the server target
# too would run it twice per stop and spend the hook's block cap at double speed. A probe on
# 2026-10-03 confirmed a `--settings` Stop hook fires under `claude -p`. The hook command
# names this repo's primary checkout by absolute path, where `.claude/settings.json` uses
# `$CLAUDE_PROJECT_DIR`: the hook's cwd follows the agent's `cd`, so the command names the
# batch's own snapshot of this repo by absolute path instead; see `snapshot_root`.
def stop_hook_settings(root: str) -> dict:
    """The `--settings` value registering `fanout-stop` from the snapshot at `root`."""
    command = f"{root}/.claude/hooks/run-hook.sh fanout-stop"
    hook = {"type": "command", "command": command, "timeout": 10}
    return {"hooks": {"Stop": [{"hooks": [hook]}]}}


# DECIDED: `RuntimeMaxSec=` here, where `claude-rc-restart.service.j2` rejects it for
# claude-rc.service. systemd records its expiry as a failure (`Result=timeout`); for a
# long-lived service host that is a false alarm, and for a batch that ran out of time it is
# the verdict `status` should print. Five hours covers the longest fan-out session measured
# (286 minutes; the next longest was 104) and bounds one that waits on something forever.
RUNTIME_MAX_S = 5 * 3600
# A `--review` batch runs up to five sessions under one unit (`fanout_lib.review`): the
# implementer, a review, a fix, a delta review and the landing. The reviews and the fix are far
# shorter than the implementer, so three hours on top of its five covers them.
# `review.LAND_MARGIN_S` skips the landing rather than start it too close to this cap.
REVIEW_RUNTIME_MAX_S = 8 * 3600
# The interpreter a unit runs repo Python with; `fanout_place.HEALTH_CMD` pins the same one.
HEADLESS_PYTHON = "3.14.6"
# The script a `--review` unit runs, relative to a checkout of this repo; see `review_script`.
REVIEW_SCRIPT = "scripts/dev/fanout_review.py"
# Another repo's batch runs this repo's code from a snapshot of `origin/master` inside its own
# worktree, never from the host's primary checkout, which can lag it (#3684, #3762). The
# snapshot holds what such a batch reads: the review pipeline and everything it imports under
# `scripts/`, the headless system prompt beside it, and the `fanout-stop` hook. It also holds
# the uv project files, so the brief's `findings.py` command can `uv run --project` the
# snapshot rather than the primary checkout (#3783). It lives under `.fanout/`, which
# `exclude_fanout_command` keeps out of git, so `clean` reads the tree as clean and removes the
# snapshot with the tree.
SNAPSHOT_DIR = ".fanout/server"
SNAPSHOT_PATHS = (
    "scripts",
    ".claude/hooks",
    "pyproject.toml",
    "uv.lock",
    ".python-version",
)


# The user manager's PATH lacks ~/.local/bin (claude, uv) and repo hooks need uv. The fnm
# default alias is where `node` lives: the dotfiles repo's `bin/gate` runs `node --test`, and an
# interactive shell finds node only through fnm's per-shell directory, which a unit never gets.
# Both sit under the launching user's HOME, which is /var/lib/claude for the agent user (#3627).
def unit_path(home: str) -> str:
    """The PATH a batch's unit runs with, for a user whose home directory is `home`."""
    return (
        f"{home}/.local/bin:{home}/.local/share/fnm/aliases/default/bin:"
        "/usr/local/bin:/usr/bin:/bin"
    )


class LaunchError(Exception):
    """A step of `launch_command`'s chain failed or timed out; str() carries the reason."""


def worktree_path(batch: str, target: Target = SERVER_TARGET) -> str:
    return f"{target.checkout}/.claude/worktrees/fanout-{batch}"


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


def create_worktree_command(batch: str, target: Target = SERVER_TARGET) -> str:
    # The lock keeps prune_worktrees.py off this tree: its `--reason` doesn't match the
    # `claude session ... (pid ... start ...)` shape prune_worktrees.session_is_alive
    # recognizes, so an unrecognized reason reads as alive and the tree survives every
    # prune until Task 10's `clean` unlocks it. Without this a merged, clean, unlocked
    # tree is removable the moment the PR lands — even while the unit is still running.
    wt = worktree_path(batch, target)
    steps = [
        exists_check_command(batch, target),
        _step(f"git -C {target.checkout} fetch origin", "fetch"),
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


def snapshot_root(worktree: str) -> str:
    """Where another repo's batch in `worktree` holds its snapshot of this repo."""
    return f"{worktree}/{SNAPSHOT_DIR}"


def snapshot_command(batch: str, target: Target, server: str = REPO) -> str:
    """Archive `origin/master`'s `SNAPSHOT_PATHS` from `server` into the batch's snapshot.

    The fetch comes first, so the snapshot is master as GitHub holds it, however far the
    checkout's own branch lags. `git archive` reads the object store, never the work tree, so
    the checkout is left exactly as it was.
    """
    root = snapshot_root(worktree_path(batch, target))
    tar = f"{root}.tar"
    base = SERVER_TARGET.base
    return _step(
        f"git -C {server} fetch --quiet origin {SERVER_TARGET.base_branch} && "
        f"mkdir -p {root} && "
        f"git -C {server} archive -o {tar} {base} {' '.join(SNAPSHOT_PATHS)} && "
        f"tar -xf {tar} -C {root} && rm {tar}",
        _SNAPSHOT_STEP,
    )


def write_brief_command(batch: str, target: Target = SERVER_TARGET) -> str:
    wt = worktree_path(batch, target)
    return _step(f"mkdir -p {wt}/.fanout && cat > {wt}/.fanout/brief.md", "brief write")


def claude_args(target: Target, worktree: str) -> str:
    """The `claude -p` command line a batch in `target`, working in `worktree`, runs.

    The system prompt file is relative to the worktree for this repo, whose worktree always
    carries it. Another repo's worktree does not, so there it is read from the batch's
    snapshot of this repo (`snapshot_root`). Another repo's batch also gets the `fanout-stop`
    hook through `--settings`, because its worktree carries no `.claude/settings.json` to
    register it.
    """
    if target.is_server:
        return CLAUDE_ARGS
    root = snapshot_root(worktree)
    prefix = CLAUDE_ARGS_PREFIX.format(
        budget=BUDGET_USD, prompt=f"{root}/{SYSTEM_PROMPT_FILE}"
    )
    settings = json.dumps(stop_hook_settings(root))
    return f"{prefix} --settings {shlex.quote(settings)}"


def review_script(batch: str, target: Target = SERVER_TARGET) -> str:
    """The `fanout_review.py` path a `--review` unit in `target` runs.

    This repo's batch runs the worktree's copy, relative to the unit's WorkingDirectory.
    `worktree add` checks that tree out at `origin/master`, so the script is there and
    current, and it imports `fanout_lib` from the same tree. The host's primary checkout
    can lag `origin/master`: on 2026-10-09 daniel-server's was 85 commits behind, predated
    the script, and every review unit placed there failed to spawn (#3684). Another repo's
    worktree does not carry the script, so its batch runs the copy in its snapshot of
    `origin/master` (`snapshot_command`), which is current for the same reason.
    """
    if target.is_server:
        return REVIEW_SCRIPT
    return f"{snapshot_root(worktree_path(batch, target))}/{REVIEW_SCRIPT}"


def review_command(
    batch: str, target: Target = SERVER_TARGET, red_green: bool = False
) -> str:
    """The unit's command for a `--review` batch: `fanout_review.py` in place of `claude -p`."""
    return (
        f"uv run --no-project --no-python-downloads --python {HEADLESS_PYTHON} "
        f"{review_script(batch, target)} --batch {batch} --repo {target.repo}"
        + (" --red-green" if red_green else "")
    )


def systemd_run_command(
    batch: str,
    target: Target = SERVER_TARGET,
    home: str | None = None,
    review: bool = False,
    red_green: bool = False,
) -> str:
    """The `systemd-run` step; `home` defaults to the launching user's own HOME."""
    wt = worktree_path(batch, target)
    home = home or str(_Path.home())
    command = claude_args(target, wt)
    if review:
        command = review_command(batch, target, red_green)
    runtime = REVIEW_RUNTIME_MAX_S if review else RUNTIME_MAX_S
    return _step(
        (
            f"systemd-run --user --unit {unit_name(batch, target)} "
            f"-p WorkingDirectory={wt} "
            f"-p StandardInput=file:{wt}/.fanout/brief.md "
            f"-p StandardOutput=file:{wt}/.fanout/report.json "
            f"-p StandardError=file:{wt}/.fanout/stderr.log "
            f"-p Environment=PATH={unit_path(home)} -p Environment=HOME={home} "
            f"-p RuntimeMaxSec={runtime} "
            f"{command}"
        ),
        "systemd-run",
    )


def prepare_command(batch: str, target: Target) -> str:
    """Another repo's worktree add+lock, its snapshot and the brief write, without the agent."""
    return " && ".join(
        [
            create_worktree_command(batch, target),
            snapshot_command(batch, target),
            write_brief_command(batch, target),
        ]
    )


def launch_command(batch: str, review: bool = False, red_green: bool = False) -> str:
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
            systemd_run_command(batch, review=review, red_green=red_green),
        ]
    )


_STEP_SENTINEL_RE = re.compile(r"^fanout-step: (.+)$", re.MULTILINE)

# Cleanup removes the worktree and its branch, so it only runs for a step that could have
# left one half-made: `worktree add`/`worktree lock` do; a `fetch` failure
# precedes both and created nothing (cleanup there would fail its own `worktree remove` with a
# confusing "not a working tree"); `brief write`/`systemd-run` come after the tree already exists and
# leave it in place for inspection instead. `exists` is the one that must never be here: it
# fails BECAUSE a tree is there, and that tree belongs to an earlier batch, not this launch.
# The snapshot step runs inside a tree this launch just made, before the claim, so a failed
# fetch or archive removes that tree too: nothing has started in it.
_SNAPSHOT_STEP = "server snapshot"
_CLEANUP_STEPS = frozenset({"worktree add", "worktree lock", _SNAPSHOT_STEP})

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
    return reason + _release(tools, batch, issues, target)


def _release(tools: Tools, batch: str, issues: list[int], target: Target) -> str:
    """Release the batch's claims; return a message suffix when the release itself failed."""
    argv = [
        "release",
        *(str(n) for n in issues),
        "--worktree",
        branch_name(batch),
        "--repo",
        target.repo,
        "--reason",
        "fan-out launch refused",
    ]
    try:
        released = tools.findings(argv)
    except subprocess.TimeoutExpired:
        return "; release timed out"
    if released.returncode != 0:
        return f"; release failed ({released.returncode})"
    return ""


def _launch_elsewhere(
    tools: Tools,
    host: str,
    batch: str,
    brief_text: str,
    issues: list[int],
    target: Target,
    review: bool = False,
) -> None:
    """Launch a batch in another repo: prepare the tree, claim, then start the agent.

    Three calls rather than `launch_command`'s one, because the claim has to fall between the
    tree and the agent. They cost no ssh connection, since `cmd_launch` pins every such batch
    to the host it runs on.

    Raises:
        LaunchError: as `launch` documents, plus a refused claim, which removes the tree.
    """
    try:
        proc = _run(tools, host, prepare_command(batch, target), brief_text, "launch")
    except LaunchError as exc:
        message = str(exc) + (_cleanup_worktree(tools, host, batch, target) or "")
        raise LaunchError(message) from None
    if proc.returncode != 0:
        _raise_failure(host, batch, target, proc, tools)
    refused = _claim(tools, batch, issues, target)
    if refused:
        cleanup = _cleanup_worktree(tools, host, batch, target) or ""
        raise LaunchError(f"claim: {refused}{cleanup}")
    # From here the claim is held, and a failure must give it back. The tree stays locked for
    # inspection, which keeps a claim under it live for good, and a first batch that fails
    # leaves no manifest naming either.
    try:
        proc = _run(
            tools,
            host,
            systemd_run_command(batch, target, review=review),
            None,
            "systemd-run",
        )
    except LaunchError as exc:
        raise LaunchError(str(exc) + _release(tools, batch, issues, target)) from None
    if proc.returncode != 0:
        released = _release(tools, batch, issues, target)
        try:
            _raise_failure(host, batch, target, proc, tools)
        except LaunchError as exc:
            raise LaunchError(str(exc) + released) from None


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
    review: bool = False,
    red_green: bool = False,
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
            failure into the same message. A `fetch`, `brief write` or
            `systemd-run` failure, or one this can't attribute, leaves the worktree as it
            found it instead.
        review: start `fanout_lib.review`'s pipeline instead of one `claude -p`.
        red_green: give that pipeline its red phase (`fanout_lib.red_gate`); this repo
            only, as `red_gate.review_flags` decides.
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
        _launch_elsewhere(tools, host, batch, brief_text, issues, target, review)
    else:
        try:
            proc = _run(
                tools,
                host,
                launch_command(batch, review, red_green),
                brief_text,
                "launch",
            )
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
