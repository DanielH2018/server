#!/usr/bin/env python3
"""SessionStart health banner — when a Claude Code session opens in this repo,
surface anything that's already broken so you don't start work blind:
  * containers that are unhealthy or stuck restarting (fast, local `docker ps`)
  * Prometheus scrape targets that are down (fleet-wide, via scripts/probe.py)

Design contract (mirrors the other hooks here):
  - SILENT when all-green: prints nothing, so it adds zero context noise on a
    healthy day. Set SESSION_HEALTH_VERBOSE=1 to force an all-clear line (demo/test).
  - READ-ONLY and NEVER BLOCKS: every external call is timeout-bounded and wrapped;
    any failure degrades to a quiet skip (or, for a wedged dockerd, a one-line
    warning — a dockerd that hangs IS the signal). Always exits 0.
  - A host with no docker binary is not a broken Docker host: daniel-box runs k3s
    and sets has_docker: false. Both checks skip silently there.
  - The docker check is local + sub-second and always runs. The Prometheus check
    goes through `uv run probe.py` (one subprocess, bounded) — SessionStart fires
    once per session, so the small cost is paid rarely; it's skipped on any error so
    a down monitoring stack can never stall session start.

Wired via .claude/settings.json -> hooks.SessionStart. Stdout is injected as
session context by Claude Code (same mechanism the remember plugin uses).
"""

import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _run(cmd, timeout):
    """Run cmd in the repo dir, capturing output. Raises on timeout/missing binary."""
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, cwd=REPO, check=False
    )


def docker_problems():
    """(lines, docker_ok): one line per unhealthy/restarting container.
    docker_ok=False (with a warning line) if dockerd is unreachable."""
    try:
        unhealthy = _run(
            [
                "docker",
                "ps",
                "--filter",
                "health=unhealthy",
                "--format",
                "{{.Names}}\t{{.Status}}",
            ],
            5,
        )
        restarting = _run(
            [
                "docker",
                "ps",
                "-a",
                "--filter",
                "status=restarting",
                "--format",
                "{{.Names}}\t{{.Status}}",
            ],
            5,
        )
    # Two clauses, not `except (A, B, C)`: ruff (3.14 target) rewrites a parenthesized tuple into
    # the unparenthesized `except A, B:` form. That is now harmless — session-health.sh runs this
    # on the pinned 3.14 via uv — but the split is kept because this file is where that bug
    # actually shipped: the wrapper sends stderr to /dev/null and exits 0, so the SyntaxError was
    # invisible until someone noticed the banner had stopped appearing.
    except subprocess.TimeoutExpired:
        return ["  ✗ docker unreachable (dockerd wedged)"], False
    except OSError:
        # FileNotFoundError (docker binary absent) is an OSError subclass. No docker binary
        # means this host is not a Docker host at all — daniel-box runs k3s and sets
        # has_docker: false — not that a Docker host is broken. Staying silent is the whole
        # point of the all-green contract; warning here would fire on every session open
        # forever. docker_ok=False still skips the Prometheus probe, which needs `docker
        # inspect` to resolve the container IP and so cannot work here either.
        return [], False
    lines = []
    for label, res in (("unhealthy", unhealthy), ("restarting", restarting)):
        for row in res.stdout.splitlines():
            if not row.strip():
                continue
            name, _, status = row.partition("\t")
            lines.append("  ✗ {} — {} ({})".format(name, label, status.strip()))
    return lines, True


def target_problems():
    """Best-effort list of down Prometheus scrape targets. [] on any failure
    (monitoring being unreachable must not block or spam session start)."""
    try:
        res = _run(["uv", "run", "python", "scripts/probe.py", "targets"], 6)
        active = json.loads(res.stdout)["data"]["activeTargets"]
    except Exception:
        return []
    bad = []
    for t in active:
        if t.get("health") == "up":
            continue
        labels = t.get("labels", {})
        job = labels.get("job", "?")
        inst = labels.get("instance", "?")
        err = (t.get("lastError") or "").strip()[:70]
        bad.append(
            "  ✗ target {} [{}] {}".format(job, inst, "— " + err if err else "down")
        )
    return bad


def other_live_sessions(cwd):
    """Lines describing the other Claude sessions working this repo right now.

    Derived from git and /proc rather than from anything a session declares, so it cannot
    go stale when a session forgets to announce itself or dies without cleaning up. Knowing
    another session is already in a role is what stops two of them editing it at once.
    """
    sys.path.insert(0, os.path.join(REPO, "scripts"))
    try:
        from prune_worktrees import parse_worktree_list, session_is_alive
    except ImportError:
        return []

    lines = []
    for tree in parse_worktree_list(
        _run(["git", "worktree", "list", "--porcelain"], 5).stdout
    )[1:]:
        if os.path.realpath(tree.path) == os.path.realpath(cwd):
            continue
        if not (tree.locked and session_is_alive(tree.lock_reason)):
            continue
        changed = _run(
            ["git", "-C", tree.path, "diff", "--name-only", "origin/master...HEAD"], 5
        ).stdout
        dirty = _run(["git", "-C", tree.path, "status", "--porcelain"], 5).stdout
        paths = sorted({p for p in changed.splitlines() if p})
        shown = ", ".join(paths[:4]) or "no commits yet"
        if len(paths) > 4:
            shown += f", +{len(paths) - 4} more"
        if dirty.strip():
            shown += " (+ uncommitted)"
        lines.append(f"  • {tree.branch or os.path.basename(tree.path)} — {shown}")
    return lines


def format_banner(problems):
    """Render the problem list as the session banner (empty string => print nothing)."""
    if not problems:
        return ""
    out = ["\U0001f3e0 Homelab health check — issues detected:"]
    out.extend(problems)
    out.append(
        "  → triage: uv run python scripts/probe.py targets | "
        "probe.py health <svc> | docker ps --filter health=unhealthy"
    )
    return "\n".join(out)


def main():
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    # Don't re-banner on mid-session compaction — only on a genuine open/resume/clear.
    if payload.get("source") == "compact":
        return 0

    dock, docker_ok = docker_problems()
    targets = target_problems() if docker_ok else []
    problems = dock + targets

    banner = format_banner(problems)
    if banner:
        print(banner)
    elif os.environ.get("SESSION_HEALTH_VERBOSE"):
        print(
            "\U0001f3e0 Homelab health: all containers healthy, all scrape targets up."
        )

    # Printed independently of health: another session's open work is information this
    # session needs even when everything is green.
    try:
        sessions = other_live_sessions(os.getcwd())
    except Exception:
        sessions = []
    if sessions:
        print("\U0001f500 Other Claude sessions in this repo:")
        for line in sessions:
            print(line)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
