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

import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from agent_logic import OpenPR, decide, delta, parse_run, render_digest, render_skip
from host_lib import atomic_write, discord_post, parse_env_file

CONFIG = "/etc/renovate-agent/config.env"
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


def worktree_is_reusable(repo_dir: str, path: str, branch: str) -> tuple[bool, str]:
    """Whether the run worktree can be thrown away and recreated.

    It cannot when the previous tick left work behind — uncommitted changes, or commits that
    never reached origin/master. Removing either would destroy a landing that was in flight,
    so the tick skips instead and names the path for the operator.
    """
    if not os.path.isdir(path):
        return True, ""
    rc, out = run(["git", "-C", path, "status", "--porcelain"])
    if rc != 0:
        return False, f"git status failed in {path}: {out.strip()[:200]}"
    if out.strip():
        return False, f"{path} has uncommitted changes from an earlier run"
    rc, out = run(
        ["git", "-C", repo_dir, "rev-list", "--count", f"origin/master..{branch}"]
    )
    if rc == 0 and out.strip() not in ("0", ""):
        return False, f"{branch} holds {out.strip()} commit(s) not on origin/master"
    return True, ""


def prepare_worktree(repo_dir: str, path: str, branch: str) -> None:
    """Recreate the run worktree at origin/master. Assumes worktree_is_reusable said yes."""
    if os.path.isdir(path):
        run(["git", "-C", repo_dir, "worktree", "remove", "--force", path])
    run(["git", "-C", repo_dir, "worktree", "prune"])
    rc, out = run(
        ["git", "-C", repo_dir, "fetch", "--quiet", "origin", "master"], timeout=300
    )
    if rc != 0:
        raise RuntimeError(f"git fetch failed: {out.strip()[:300]}")
    rc, out = run(
        ["git", "-C", repo_dir, "worktree", "add", "-B", branch, path, "origin/master"],
        timeout=300,
    )
    if rc != 0:
        raise RuntimeError(f"git worktree add failed: {out.strip()[:300]}")


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


if __name__ == "__main__":
    sys.exit(main())
