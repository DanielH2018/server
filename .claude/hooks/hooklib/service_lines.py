"""The SessionStart banner's service sections: unhealthy containers and down scrape targets.

Split out of session-health.py, which sat at exactly its 600-line cap with no headroom
(ansible/tests/_ratchet.py), the same reason `worktree_lines` was split before it. Package
name is `hooklib`, not `lib`, so it never shares a namespace-package name with `scripts/lib`
(see session-health.py's own import comment).

Every function here takes its subprocess runner as an argument rather than importing one.
That is what lets `session-health.py` keep a `_run` bound to the repo directory while a test
drives these against a table of canned results, without patching a first-party module.
"""

import json
import os
import subprocess


def docker_problems(run):
    """One line per unhealthy or restarting container, as (lines, docker_ok).

    docker_ok is False, with a warning line, when dockerd is unreachable.

    Args:
        run: `session-health.py`'s `_run` — takes (argv, timeout), raises on timeout or a
            missing binary.
    """
    try:
        unhealthy = run(
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
        restarting = run(
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
    # Two clauses, not `except (A, B, C)`: ruff (3.14 target) rewrites a parenthesized tuple
    # into the unparenthesized `except A, B:` form. That is now harmless — session-health.sh
    # runs this on the pinned 3.14 via uv — but the split is kept because session-health.py is
    # where that bug actually shipped: its wrapper sends stderr to /dev/null and exits 0, so
    # the SyntaxError was invisible until someone noticed the banner had stopped appearing.
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


def k8s_namespace(repo):
    """Return k8s_namespace from the same plaintext inventory file probe.py reads it from.

    Duplicated rather than imported — `target_problems` shells out to probe.py rather than
    importing it (see its own docstring), and this stays consistent with that.
    """
    path = os.path.join(repo, "ansible", "inventory", "group_vars", "all.yml")
    try:
        with open(path) as f:
            for line in f:
                if line.startswith("k8s_namespace:"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        return None
    return None


def is_scaled_to_zero(run, job, namespace):
    """True only if `job`'s backing Deployment is confirmed to have `spec.replicas: 0`.

    That is an on-demand game server (terraria-stats, valheim-stats) deliberately left idle,
    not a failure. Any lookup failure (wrong kind, missing Deployment, kubectl error,
    timeout) returns False: a down target we can't explain to be intentional stays reported
    rather than silently swallowed.
    """
    if not namespace:
        return False
    try:
        res = run(
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


def target_problems(run, repo, namespace=None, scaled_to_zero=is_scaled_to_zero):
    """Return down Prometheus scrape targets, minus any deliberately scaled to 0 replicas.

    Best-effort: returns [] on any failure, since monitoring being unreachable must not
    block or spam session start.

    Args:
        run: the subprocess runner, as in `docker_problems`.
        repo: the checkout to read `k8s_namespace` from.
        namespace: the namespace to ask about, read from `repo` when not given.
        scaled_to_zero: `is_scaled_to_zero`-shaped — a seam, so a test can decide which job
            is intentionally idle without a live cluster.
    """
    try:
        res = run(["uv", "run", "python", "scripts/diagnostics/probe.py", "targets"], 6)
        active = json.loads(res.stdout)["data"]["activeTargets"]
    except Exception:
        return []
    if namespace is None:
        namespace = k8s_namespace(repo)
    bad = []
    for t in active:
        if t.get("health") == "up":
            continue
        labels = t.get("labels", {})
        job = labels.get("job", "?")
        if scaled_to_zero(run, job, namespace):
            continue
        inst = labels.get("instance", "?")
        err = (t.get("lastError") or "").strip()[:70]
        bad.append(
            "  ✗ target {} [{}] {}".format(job, inst, "— " + err if err else "down")
        )
    return bad
