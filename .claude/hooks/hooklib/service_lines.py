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
import re
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


# Services in a stale-release verdict named on the banner line after "e.g.".
_STALE_NAMED = 3

# The headline `bridge.msgfmt._render` puts first in every DOWN verdict: a count, the unit,
# then `stale` followed by `: ` (the one-service shape) or ` — ` (both plural shapes). The
# space inside the character class is load-bearing — tidying it to `[:—]` drops both plural
# shapes into the unrecognised branch, which is the bug this pattern exists to end (#2390).
_STALE_HEADLINE = re.compile(r"^(\d+) services? stale[: —]")

# The one shape that carries a service name outside a parenthesised group.
_ONE_STALE = re.compile(r"^1 service stale: (\S+) —")

# `<reason> (<k>: a, b, +N)` — the per-reason name list of the multi-reason shape.
_COUNTED_GROUP = re.compile(r"\((\d+): ([^)]*)\)")

_PARENTHESISED = re.compile(r"\(([^)]*)\)")

# A deploy tag is a role directory name: lowercase, digits and hyphens. This is what keeps a
# reason's own parenthetical out of the name list — `group_vars/all.yml (lan_subnet)` holds
# an underscore, so `lan_subnet` is rejected where `home-assistant` is kept.
_SERVICE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# What `release-staleness-check.sh` prepends when probe.py exits neither 0 nor 1.
_PROBE_FAILED_PREFIX = "probe.py releases --stale-only exited "


def stale_verdict_names(verdict):
    """Service names recovered from a kuma stale verdict, in order, possibly empty.

    Best-effort by construction: `bridge.msgfmt.format_down` shrinks the names shown per
    reason from five to one to fit 900 chars, and cuts the line from the right when even
    that does not fit. The count on the headline survives all of that, so the caller takes
    the count from there and treats these as examples.

    Args:
        verdict: the journal line with `status=down ` already stripped.

    Returns:
        The names that parse as deploy tags, de-duplicated, first occurrence first.
    """
    body = verdict.split(". Details:")[0]
    groups = _COUNTED_GROUP.findall(body)
    one = _ONE_STALE.match(body)
    if groups:
        raw = [name for _count, names in groups for name in names.split(",")]
    elif one:
        raw = [one.group(1)]
    else:
        # `N services stale — <reason> (a, b, c)`: the names are the LAST parenthesised
        # group, because the reason ahead of them carries parentheses of its own.
        tail = _PARENTHESISED.findall(body)
        raw = tail[-1].split(",") if tail else []
    names = []
    for name in (candidate.strip() for candidate in raw):
        if _SERVICE_NAME.match(name) and name not in names:
            names.append(name)
    return names


def stale_release_problems(run):
    """One line naming the services `Release Staleness Drift` last found stale, or nothing.

    `release-staleness-check.sh` (roles/setup/k3s) logs `status=<up|down> <verdict>` under
    its own journal tag every `k3s_release_staleness_cron_minute`, then pushes the same text
    to Kuma. That journal entry is the one place the verdict exists on the host: reading the
    release records here instead would re-run the git derivation the cron just paid for, and
    the banner must not (#1993). `--since -2h` keeps a dead cron's last verdict off the
    banner — the Kuma tile pages for the dead cron itself. A host that never runs the cron
    (an agent node, a laptop) has no such entry and stays silent, like a host with no docker.

    The verdict is `probe.py releases --stale-only --kuma`'s output, one line in one of
    `bridge.msgfmt`'s three DOWN shapes. Three branches read it: the headline makes it a
    stale verdict, the `exited N:` prefix makes it the check breaking, and anything else is
    a verdict neither this parser nor the producer agrees on — also shown as broken. That
    last branch cannot be dropped. The cron maps a probe exit of 1 to `status=down` with
    the output unmodified, and an uncaught Python exception exits 1 too, so a traceback and
    a real stale list reach the journal under the same status with no `exited N:` prefix
    between them; the text shape is the only thing that tells them apart.

    Args:
        run: the subprocess runner, as in `docker_problems`.
    """
    argv = ["journalctl", "-t", "release-staleness-check", "-n", "1", "-o", "cat"]
    try:
        res = run(argv + ["--since", "-2h", "--no-pager"], 5)
    except subprocess.TimeoutExpired, OSError:
        return []
    lines = res.stdout.strip().splitlines() if res.returncode == 0 else []
    if not lines or not lines[0].startswith("status=down "):
        return []
    verdict = lines[0][len("status=down ") :]
    if verdict.startswith(_PROBE_FAILED_PREFIX):
        return [f"  ⚠ release staleness check is broken: {verdict[:110]}"]
    head = _STALE_HEADLINE.match(verdict)
    if not head:
        return [
            "  ⚠ release staleness check returned an unrecognised verdict: "
            f"{verdict[:110]}"
        ]
    count = int(head.group(1))
    names = stale_verdict_names(verdict)
    # "e.g." even when the names happen to cover the count: the count comes off the headline
    # and is authoritative, while the names are whatever survived a message msgfmt may have
    # shrunk to one name per reason or cut from the right.
    named = f", e.g. {', '.join(names[:_STALE_NAMED])}" if names else ""
    return [
        "  ⚠ release staleness: {} {} {} manifests behind origin/master{} — "
        "uv run python scripts/diagnostics/probe.py releases --stale-only".format(
            count,
            "service" if count == 1 else "services",
            "runs" if count == 1 else "run",
            named,
        )
    ]
