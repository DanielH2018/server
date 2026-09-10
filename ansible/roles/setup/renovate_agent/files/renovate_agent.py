#!/usr/bin/env python3
"""Unattended Renovate agent — runs once per daily systemd-timer tick.

Spends one headless Claude Code session (`claude -p "/renovate-prs"`) on the repo's open
Renovate PRs, then posts a Discord digest keyed on the MEASURED change in the open-PR set.
Writes a last_run timestamp for the "Renovate Agent — Alive" Kuma monitor.

The tick is gated before it costs anything: no open Renovate PRs means no session, and a
GitOps hold means no session either. Both decisions live in agent_logic, which is pure.

The session runs in a dedicated worktree, never in the primary checkout. One untracked file
in /home/<user>/server parks the GitOps deployer silently, and a session that edits, renders
and tests is guaranteed to leave some.

Config from /etc/renovate-agent/config.env (KEY=VALUE) — see templates/config.env.j2.
Stdlib only.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import json
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from agent_logic import OpenPR, decide, delta, parse_run, render_digest, render_skip
from host_lib import atomic_write, discord_post, parse_env_file

# The env override exists so the I/O shell can be exercised end-to-end against a throwaway
# config before the timer is ever armed. Without it the only way to run this code is to deploy
# it, and the first armed tick would be its first execution.
CONFIG = os.environ.get("RENOVATE_AGENT_CONFIG", "/etc/renovate-agent/config.env")
USER_AGENT = "renovate-agent"

# Written by gitops_deploy.py. Read, never written, here.
HOLD_FILE = "/var/lib/gitops-deploy/hold_sha"
HOLD_PLANE_FILE = "/var/lib/gitops-deploy/hold_plane"


def log(msg: str) -> None:
    print(f"[renovate-agent] {msg}", flush=True)


def read_file(path: str) -> str:
    try:
        with open(path) as fh:
            return fh.read()
    except OSError:
        return ""


def run(argv: list[str], cwd: str | None = None, timeout: int = 120) -> tuple[int, str]:
    """Run `argv`, returning (returncode, stdout+stderr). Never raises on a non-zero exit."""
    try:
        p = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s: {' '.join(argv)}"
    except OSError as e:
        return 127, str(e)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def open_prs(repo: str) -> list[OpenPR]:
    """The open Renovate PRs, newest first. An empty list on any gh failure is NOT a census.

    Raising rather than returning [] matters: a failed `gh` call that read as "no open PRs"
    would make the tick skip quietly, which is indistinguishable from the healthy steady state.
    """
    rc, out = run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--author",
            "app/renovate",
            "--state",
            "open",
            "--limit",
            "50",
            "--json",
            "number,title,url",
        ]
    )
    if rc != 0:
        raise RuntimeError(f"gh pr list failed (exit {rc}): {out.strip()[:300]}")
    return [
        OpenPR(number=int(p["number"]), title=p.get("title", ""), url=p.get("url", ""))
        for p in json.loads(out)
    ]


@dataclass(frozen=True)
class AgentTools:
    """Every process boundary the worktree and crash-report paths cross, as one object.

    A test replaces a field rather than a module attribute, which is what
    `ansible/tests/repo/test_module_length_ratchet.py` requires of a module under test — a
    `monkeypatch.setattr` on this module would pin its name into the test. The defaults are
    production, so `main()` passes nothing.

    `open_prs` and `run_session` stay out: their argv is what the suite asserts on, and a field
    there would replace the builder rather than the process. Same reasoning as
    `deploy_toolbox.DeployTools`, which this mirrors.
    """

    run: Callable[..., tuple[int, str]] = run
    discord_post: Callable[..., bool] = discord_post
    rmtree: Callable[..., None] = shutil.rmtree


TOOLS = AgentTools()


def is_registered_worktree(repo_dir: str, path: str, tools: AgentTools = TOOLS) -> bool:
    """Whether git actually knows about the directory at `path`.

    A directory can sit at the run worktree's path with no git metadata at all — that is what a
    killed session leaves behind, and it is what stopped this agent for two days from
    2026-09-08 (#1477). It matters because **git searches UPWARD for a repository**: a
    `git -C <orphan dir> status` resolves to the PRIMARY CHECKOUT and answers about that tree
    instead, so every check made from inside such a directory is about the wrong repo.

    Compared by resolved path, since `git worktree list --porcelain` prints the real path and
    `repo_dir` may be reached through a symlink.
    """
    rc, out = tools.run(["git", "-C", repo_dir, "worktree", "list", "--porcelain"])
    if rc != 0:
        # Fail closed: unknown means "treat it as git's", so nothing removes a tree that may be
        # registered. The caller's own error path then names the path for an operator.
        return True
    want = os.path.realpath(path)
    return any(
        os.path.realpath(line[len("worktree ") :].strip()) == want
        for line in out.splitlines()
        if line.startswith("worktree ")
    )


def worktree_is_reusable(
    repo_dir: str, path: str, branch: str, tools: AgentTools = TOOLS
) -> tuple[bool, str]:
    """Whether the run worktree can be thrown away and recreated.

    It cannot when the previous tick left work behind — uncommitted changes, or commits that
    never reached origin/master. Removing either would destroy a landing that was in flight,
    so the tick skips instead and names the path for the operator.

    SCOPE (see lib.git.git_dirty, #1223): whole tree, untracked counted — an unlanded scratch
    file is exactly the kind of leftover this must not discard. Stays inline rather than
    importing lib.git because this file is deployed onto the host with no path to scripts/lib.
    """
    if not os.path.isdir(path):
        return True, ""
    if not is_registered_worktree(repo_dir, path, tools):
        # Debris, not a worktree: `git -C path status` would search upward and report on the
        # PRIMARY CHECKOUT, so a dirty primary would read as "this run tree has uncommitted
        # changes" and a clean one would wave through a directory `worktree add` then refuses.
        # It holds no work git can see, so prepare_worktree reclaims it.
        return True, ""
    rc, out = tools.run(["git", "-C", path, "status", "--porcelain"])
    if rc != 0:
        return False, f"git status failed in {path}: {out.strip()[:200]}"
    if out.strip():
        return False, f"{path} has uncommitted changes from an earlier run"
    rc, out = tools.run(
        ["git", "-C", repo_dir, "rev-list", "--count", f"origin/master..{branch}"]
    )
    if rc == 0 and out.strip() not in ("0", ""):
        return False, f"{branch} holds {out.strip()} commit(s) not on origin/master"
    return True, ""


def _process_start_time(pid: int) -> str:
    """The starttime field scripts/dev/prune_worktrees.py's session_is_alive() compares.

    Read from /proc/<pid>/stat: starttime is the field after the last ')', at index 19 once
    split on whitespace — the comm field can itself contain spaces or parens, which is why
    session_is_alive() anchors on the final ')' rather than counting from the start. Returns
    "" if the pid can't be read, which makes the lock reason fail LOCK_OWNER's regex and so
    read as "someone else's format" — session_is_alive() treats that as alive, never as dead.
    """
    stat = read_file(f"/proc/{pid}/stat")
    fields = stat.rpartition(")")[2].split()
    return fields[19] if len(fields) > 19 else ""


def _lock_reason() -> str:
    """The reason string `git worktree lock` records, in the format LOCK_OWNER parses.

    scripts/dev/prune_worktrees.py keeps a worktree only while `tree.locked and
    session_is_alive(tree.lock_reason)` — session_is_alive() matches `(pid <n> start <n>)`
    against /proc/<pid>/stat, so this must be this process's own pid and start time. As long
    as this script is still running (it blocks on run_session() for the run's duration), the
    pid+start pair keeps matching and the pruner keeps the tree; once the process exits, the
    pid no longer matches (or is reused with a different start time) and the lock is ignored.
    """
    pid = os.getpid()
    return f"renovate-agent (pid {pid} start {_process_start_time(pid)})"


def prepare_worktree(
    repo_dir: str, path: str, branch: str, tools: AgentTools = TOOLS
) -> None:
    """Recreate the run worktree at origin/master. Assumes worktree_is_reusable said yes."""
    if os.path.isdir(path):
        if is_registered_worktree(repo_dir, path, tools):
            # The previous tick's lock is still on this tree — `worktree remove` refuses a
            # locked tree outright, and passing --force once does not override a lock (git
            # needs it twice). Unlock first, mirroring prune_worktrees.py's own remove().
            tools.run(["git", "-C", repo_dir, "worktree", "unlock", path])
            tools.run(["git", "-C", repo_dir, "worktree", "remove", "--force", path])
        else:
            # An unregistered directory is a recoverable state, not a crash. `worktree remove`
            # refuses a path it has no record of, the return code was discarded, and the
            # following `worktree add` then died on `fatal: ... already exists` — which killed
            # every tick from 2026-09-08 to 2026-09-10 and left the alive monitor to expire
            # (#1477). git holds no record of it, so there is no branch and no commit to lose;
            # `worktree_is_reusable` above has already refused every case that does.
            log(f"reclaiming {path}: a directory git has no worktree record of")
            tools.rmtree(path, ignore_errors=True)
            if os.path.isdir(path):
                raise RuntimeError(f"could not remove the orphaned directory {path}")
    tools.run(["git", "-C", repo_dir, "worktree", "prune"])
    rc, out = tools.run(
        ["git", "-C", repo_dir, "fetch", "--quiet", "origin", "master"], timeout=300
    )
    if rc != 0:
        raise RuntimeError(f"git fetch failed: {out.strip()[:300]}")
    rc, out = tools.run(
        ["git", "-C", repo_dir, "worktree", "add", "-B", branch, path, "origin/master"],
        timeout=300,
    )
    if rc != 0:
        raise RuntimeError(f"git worktree add failed: {out.strip()[:300]}")
    # Lock the tree for the run's duration so a concurrent session's SessionStart pruner
    # (prune_worktrees.py --prune) doesn't delete it out from under this run — see #1069.
    rc, out = tools.run(
        ["git", "-C", repo_dir, "worktree", "lock", path, "--reason", _lock_reason()]
    )
    if rc != 0:
        raise RuntimeError(f"git worktree lock failed: {out.strip()[:300]}")


def run_session(cfg: dict[str, str], cwd: str, log_path: str) -> tuple[str, int, bool]:
    """Run the headless Claude session in `cwd`, teeing its stdout to `log_path`.

    stdin is /dev/null: without it Claude Code waits 3s for piped input on every start, and
    under systemd there is no stdin to wait for.
    """
    prompt = read_file(cfg["PROMPT_FILE"])
    if not prompt.strip():
        raise RuntimeError(f"prompt file {cfg['PROMPT_FILE']} is empty or missing")
    argv = [
        cfg.get("CLAUDE_BIN", "claude"),
        "-p",
        prompt,
        "--permission-mode",
        cfg.get("PERMISSION_MODE", "auto"),
        "--model",
        cfg.get("MODEL", "opus"),
        "--output-format",
        "json",
        "--max-budget-usd",
        cfg.get("BUDGET_USD", "25"),
    ]
    timeout = int(cfg.get("RUN_TIMEOUT_S", "5400"))
    log(f"starting session (timeout {timeout}s, budget ${cfg.get('BUDGET_USD', '25')})")
    try:
        p = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as e:
        atomic_write(log_path, (e.stdout or "") if isinstance(e.stdout, str) else "")
        return "", 124, True
    atomic_write(log_path, p.stdout or "")
    return p.stdout or "", p.returncode, False


def main() -> int:
    cfg = parse_env_file(CONFIG)
    missing = [k for k in ("REPO", "REPO_DIR", "PROMPT_FILE") if not cfg.get(k)]
    if missing:
        raise RuntimeError(f"{CONFIG} is missing {', '.join(missing)}")
    host = os.uname().nodename
    webhook = cfg.get("DISCORD_WEBHOOK", "")
    state_dir = cfg.get("STATE_DIR", "/var/lib/renovate-agent")
    repo_dir = cfg["REPO_DIR"]
    branch = cfg.get("BRANCH", "worktree-renovate-auto")
    path = os.path.join(
        repo_dir, ".claude", "worktrees", cfg.get("WORKTREE", "renovate-auto")
    )
    log_path = os.path.join(state_dir, "last_session.json")

    before = open_prs(cfg["REPO"])
    gate = decide(before, read_file(HOLD_FILE), read_file(HOLD_PLANE_FILE))
    if not gate.run:
        log(f"skipping: {gate.reason}")
        if not gate.quiet:
            discord_post(webhook, render_skip(gate, host), USER_AGENT, log=log)
        return 0

    reusable, why = worktree_is_reusable(repo_dir, path, branch)
    if not reusable:
        # Not a failure: the previous tick left work in flight. Say so and leave it alone —
        # removing the worktree is how unlanded work is lost.
        msg = f"renovate-agent: skipped on {host} — {why}. Clear it, then the next tick runs."
        log(msg)
        discord_post(webhook, msg, USER_AGENT, log=log)
        return 0

    log(f"{gate.reason}; preparing {path}")
    prepare_worktree(repo_dir, path, branch)
    stdout, rc, timed_out = run_session(cfg, path, log_path)
    outcome = parse_run(stdout, rc, timed_out)

    after = open_prs(cfg["REPO"])
    moved = delta(before, after)
    log(f"resolved={moved.resolved} remaining={moved.remaining} ok={outcome.ok}")
    discord_post(
        webhook, render_digest(outcome, moved, host, log_path), USER_AGENT, log=log
    )

    atomic_write(os.path.join(state_dir, "last_run"), str(int(time.time())))
    # A session that failed is a unit failure, so OnFailure pages as well as the digest.
    return 0 if outcome.ok else 1


def report_crash(
    exc: BaseException, tools: AgentTools = TOOLS, config_path: str = CONFIG
) -> None:
    """Push a `down` carrying the exception text, and post the same to Discord.

    The alive beat is the unit's `ExecStartPost`, which systemd runs only when the process
    exited 0 — so a crash pushed NOTHING and the monitor went down 28 hours later by deadman
    expiry, carrying no reason. An operator then reads "no heartbeat", which is what a host that
    is simply off looks like: for two days from 2026-09-08 that hid a one-line
    `prepare_worktree` failure (#1477). The deadman stays as the backstop for a host that is
    genuinely off; it must not be the only signal for a run that started and threw.

    Best effort throughout, and it never re-raises: a failed report must not replace the
    traceback that says what actually broke. The push URL carries a rotation-tracked token, so
    it is read from config.env (0600) and never logged.
    """
    text = f"{type(exc).__name__}: {exc}"
    try:
        cfg = parse_env_file(config_path)
    except OSError:
        cfg = {}
    push_url = cfg.get("KUMA_PUSH_URL", "")
    if push_url:
        sep = "&" if "?" in push_url else "?"
        tools.run(
            [
                "curl",
                "-fsS",
                "-m",
                "15",
                "-o",
                "/dev/null",
                "--get",
                "--data-urlencode",
                f"msg=crashed: {text[:200]}",
                f"{push_url}{sep}status=down",
            ],
            timeout=30,
        )
    tools.discord_post(
        cfg.get("DISCORD_WEBHOOK", ""),
        f"🚨 renovate-agent: CRASHED on {os.uname().nodename} — {text[:400]}",
        USER_AGENT,
        log=log,
    )


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        report_crash(e)
        raise
