#!/usr/bin/env python3
"""SessionStart health banner — when a Claude Code session opens in this repo,
surface anything that's already broken so you don't start work blind:
  * containers that are unhealthy or stuck restarting (fast, local `docker ps`)
  * Prometheus scrape targets that are down (fleet-wide, via scripts/diagnostics/probe.py)
  * this branch sitting behind origin/master's last-fetched ref (local-only, no `git fetch`)

Design contract (mirrors the other hooks here):
  - SILENT when all-green: prints nothing, so it adds zero context noise on a
    healthy day. Set SESSION_HEALTH_VERBOSE=1 to force an all-clear line (demo/test).
  - READ-ONLY and NEVER BLOCKS: every external call is timeout-bounded and wrapped;
    any failure degrades to a quiet skip (or, for a wedged dockerd, a one-line
    warning — a dockerd that hangs IS the signal). Always exits 0.
  - A host with no docker binary is not a broken Docker host: daniel-box runs k3s
    and sets has_docker: false. The docker check skips silently there; the Prometheus
    check does NOT depend on docker (it goes through probe.py's cluster route, not
    `docker inspect`) and always runs regardless.
  - The docker check is local + sub-second and always runs. The Prometheus check
    goes through `uv run probe.py` (one subprocess, bounded) — SessionStart fires
    once per session, so the small cost is paid rarely; it's skipped on any error so
    a down monitoring stack can never stall session start. A down target whose
    Deployment is deliberately scaled to 0 replicas (an on-demand game server) is
    filtered out rather than reported — otherwise this would trade a false all-clear
    for the same two names on every session open forever.

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
        # forever.
        return [], False
    lines = []
    for label, res in (("unhealthy", unhealthy), ("restarting", restarting)):
        for row in res.stdout.splitlines():
            if not row.strip():
                continue
            name, _, status = row.partition("\t")
            lines.append("  ✗ {} — {} ({})".format(name, label, status.strip()))
    return lines, True


def _k8s_namespace():
    """k8s_namespace, read from the same plaintext inventory file scripts/diagnostics/probe.py reads it
    from. Duplicated rather than imported — target_problems() shells out to probe.py rather
    than importing it (see its own docstring), and this stays consistent with that."""
    path = os.path.join(REPO, "ansible", "inventory", "group_vars", "all.yml")
    try:
        with open(path) as f:
            for line in f:
                if line.startswith("k8s_namespace:"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        return None
    return None


def _is_scaled_to_zero(job, namespace):
    """True only if `job`'s backing Deployment is confirmed to have spec.replicas: 0 — an
    on-demand game server (terraria-stats, valheim-stats) deliberately left idle, not a
    failure. Any lookup failure (wrong kind, missing Deployment, kubectl error, timeout)
    returns False: a down target we can't explain to be intentional stays reported rather
    than silently swallowed."""
    if not namespace:
        return False
    try:
        res = _run(
            [
                "k3s",
                "kubectl",
                "-n",
                namespace,
                "get",
                "deployment",
                job,
                "-o",
                "jsonpath={.spec.replicas}",
            ],
            5,
        )
    except subprocess.TimeoutExpired, OSError:
        return False
    if res.returncode != 0:
        return False
    try:
        return int(res.stdout.strip()) == 0
    except ValueError:
        return False


def target_problems():
    """Best-effort list of down Prometheus scrape targets, minus any whose Deployment is
    deliberately scaled to 0 replicas. [] on any failure (monitoring being unreachable must
    not block or spam session start)."""
    try:
        res = _run(
            ["uv", "run", "python", "scripts/diagnostics/probe.py", "targets"], 6
        )
        active = json.loads(res.stdout)["data"]["activeTargets"]
    except Exception:
        return []
    namespace = _k8s_namespace()
    bad = []
    for t in active:
        if t.get("health") == "up":
            continue
        labels = t.get("labels", {})
        job = labels.get("job", "?")
        if _is_scaled_to_zero(job, namespace):
            continue
        inst = labels.get("instance", "?")
        err = (t.get("lastError") or "").strip()[:70]
        bad.append(
            "  ✗ target {} [{}] {}".format(job, inst, "— " + err if err else "down")
        )
    return bad


def master_moved_problems():
    """One line when this branch is behind origin/master's remote-tracking ref, else [].

    `git rev-list --count HEAD..origin/master` reads only the local object store -- no
    `git fetch` runs here, so this can only ever be as current as the last fetch this
    checkout happened to do (a login shell's periodic fetch, a manual `git fetch`, an
    earlier `deploy.sh` run). That is the honest answer for a read-only, always-runs
    SessionStart hook: a live check would mean a network call on every session open. The
    line says "last fetched" rather than claiming to be current, and deploy.sh's own
    staleness gate (scripts/deploy_tools/deploy_staleness.py, exit 4) is what actually refuses a stale
    deploy -- this is only the earlier, cheaper warning. [] on any failure, including a
    checkout with no origin/master ref at all -- diagnosing that is not this hook's job.
    """
    try:
        res = _run(["git", "rev-list", "--count", "HEAD..origin/master"], 5)
        if res.returncode != 0:
            return []
        count = int(res.stdout.strip())
    except Exception:
        return []
    if count <= 0:
        return []
    plural = "s" if count != 1 else ""
    return [
        f"  ⚠ this branch is {count} commit{plural} behind origin/master (as of the last "
        "fetch -- `git fetch` to refresh; a deploy from here would be refused, see "
        "scripts/deploy_tools/deploy_staleness.py)"
    ]


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
        "  → triage: uv run python scripts/diagnostics/probe.py targets | "
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

    dock, _docker_ok = docker_problems()
    targets = target_problems()
    master_moved = master_moved_problems()
    problems = dock + targets + master_moved

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
