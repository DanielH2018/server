#!/usr/bin/env python3
"""Shared I/O-shell helpers for the host-run scripts (gitops_deploy.py, renovate_notify.py,
janitorr_health.py, configarr_health.py).

Each runs via ``uv run --no-project --python <pin>`` (host_python_version in
ansible/inventory/group_vars/all.yml) or directly under cron, and is deployed into its own
``/opt`` dir, where it does a ``sys.path.insert(0, <own dir>)`` so ``from host_lib import ...``
resolves the copy sitting alongside. Single source of truth for helpers that had drifted between
scripts: the Cloudflare-1010 User-Agent on the Discord POST, the torn-write-safe atomic state
write, the config.env parser, and the kubectl runner below. Stdlib only.
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.request

# Cron inherits neither a useful PATH nor KUBECONFIG. `k3s` and `kubectl` both live in
# /usr/local/bin, which the cron default omits, so a caller that does not fix this gets an
# OSError that reads like a missing binary rather than a missing PATH.
LOCAL_BIN = "/usr/local/bin"

# Distinct from any exit code kubectl itself returns, so a caller can tell "the cluster said no"
# from "we never reached the cluster".
KUBECTL_TIMEOUT_RC = 124
KUBECTL_UNRUNNABLE_RC = 125


def parse_env_file(path: str) -> dict[str, str]:
    """Parse a ``KEY=VALUE`` ``config.env`` — skips blank lines and ``#`` comments, splits on the
    first ``=`` (so a value may itself contain ``=``)."""
    out: dict[str, str] = {}
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k] = v
    return out


def atomic_write(path: str, text: str) -> None:
    """Write ``text`` to ``path`` via a temp file + ``os.replace`` so a concurrent reader never sees
    a half-written file. monitor-bridge reads these marker/state files every 300s with no retry and
    ``float()``s an empty read into a false "unparseable" DOWN page — the torn-write class 58056d18
    closed for the shell state writers, applied to the Python twins."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(text)
    os.replace(tmp, path)


def discord_post(
    webhook: str, content: str, user_agent: str, log=None, marker: str = ""
) -> bool:
    """POST ``content`` to a Discord ``webhook``. Returns True ONLY on a confirmed 2xx, so a caller
    can gate a per-SHA dedupe marker/fingerprint on it — a transient failure returning True would
    advance the marker and permanently suppress that alert. A ``user_agent`` is REQUIRED: Discord is
    behind Cloudflare, which 403s the default python-urllib UA (error 1010). An empty webhook or any
    error returns False (so the caller retries next run) and never raises, so alerting can't crash the
    caller. ``log`` (optional callable) is called with a one-line reason on skip/failure.

    ``marker`` (optional) is prepended to the posted message so the automation's output is
    self-identifying in a shared channel — the ``user_agent`` is a header-only marker Discord never
    renders. Every automation's Discord message should carry a stable ``<automation>:`` identifier,
    either via this arg or baked into ``content`` (as gitops_deploy / renovate_notify already do)."""
    if not webhook:
        if log:
            log("no Discord webhook set; skipping post")
        return False
    message = f"{marker} {content}" if marker else content
    data = json.dumps({"content": message[:1900]}).encode()
    req = urllib.request.Request(
        webhook,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": user_agent},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except Exception as e:  # alerting must never crash the caller
        if log:
            log("discord post failed: %s" % e)
        return False


def kubectl_runner(binary: str, namespace: str, timeout: int):
    """Return a `kubectl(*args) -> (rc, output)` bound to one binary, namespace and timeout.

    janitorr_health.py and configarr_health.py each carried a byte-identical copy of this,
    differing only in their env-var prefix. Two copies of a subprocess wrapper drift where it
    matters least visibly — in which failures they distinguish — so this is the single source,
    the same reasoning that put the Discord POST and the atomic write here.

    `output` is stdout on success and stderr on failure, so a caller can report the reason
    without branching. The two failure codes are distinct from anything kubectl returns, which
    lets a caller tell "the cluster said no" from "we never reached the cluster".

    PATH is fixed here rather than by each caller. Cron inherits neither PATH nor KUBECONFIG,
    and both `k3s` and `kubectl` live in /usr/local/bin, which cron's default omits — so
    without this the call raises an OSError that reads like a missing binary. KUBECONFIG stays
    the caller's job: it is a credential choice, and every one of these scripts wants the
    read-only ServiceAccount rather than whatever the environment happens to hold.
    """
    argv = binary.split()

    def kubectl(*args) -> tuple[int, str]:
        env = dict(os.environ)
        path = env.get("PATH", "")
        if LOCAL_BIN not in path.split(":"):
            env["PATH"] = f"{LOCAL_BIN}:{path}" if path else LOCAL_BIN
        try:
            proc = subprocess.run(
                [*argv, "-n", namespace, *args],
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return KUBECTL_TIMEOUT_RC, "kubectl timed out after %ss" % timeout
        except OSError as e:
            return KUBECTL_UNRUNNABLE_RC, "could not run kubectl: %s" % e
        return proc.returncode, proc.stdout if proc.returncode == 0 else proc.stderr

    return kubectl
