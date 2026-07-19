#!/usr/bin/env python3
"""Shared I/O-shell helpers for the host-run setup notifiers (gitops_deploy.py, renovate_notify.py).

Both run under the deploy host's ``/usr/bin/python3`` (3.12 floor — keep this file 3.12-clean; see
ansible/tests/test_host_scripts_py312.py) and are deployed into their own ``/opt`` dir, where each
does a ``sys.path.insert(0, <own dir>)`` so ``from host_lib import ...`` resolves the copy sitting
alongside. Single source of truth for the three helpers that had drifted between the two scripts
(the Cloudflare-1010 User-Agent on the Discord POST, the torn-write-safe atomic state write, the
config.env parser). Stdlib only.
"""

from __future__ import annotations

import json
import os
import urllib.request


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
