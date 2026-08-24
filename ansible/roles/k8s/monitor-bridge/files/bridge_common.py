#!/usr/bin/env python3
"""bridge_common — shared plumbing for monitor-bridge's check.py and autofix-bridge's autofix.py.

Both are stdlib-only sidecars (python:3.14-alpine) that read config from env vars, log
timestamped lines to stdout, push a status to an Uptime Kuma push monitor every cycle, touch a
heartbeat file for the container's liveness probe, and run the same cycle/heartbeat/sleep loop.
This module holds exactly that surface — nothing check-specific (Prometheus/Loki/B2 querying,
the CHECKS registry) or autofix-specific (the *arr classification logic) belongs here.

Deployed as a ConfigMap sibling of check.py/autofix.py — see each role's tasks/main.yml. The
canonical source lives here, in monitor-bridge/files/; autofix-bridge's tasks copy it in, the
same pattern roles/setup/common/files/host_lib.py uses for gitops_deploy/renovate_notify.
"""

import json
import os
import time
import urllib.parse
import urllib.request


def _env(name, default):
    return os.environ.get(name, default)


def sanitize(s, maxlen=120):
    """Neutralize adversary-controlled text before it enters a Discord-bound alert msg.

    Release titles, indexer names, n8n workflow names and *arr statusMessages are all
    attacker-influenced text that ends up in a Kuma push msg or a Discord report. Kuma forwards
    the msg to Discord, which renders @mentions and markdown, so collapse whitespace, defuse
    '@' (which forms @everyone/@here/user pings) and backticks, and cap the length.
    """
    s = "?" if s is None else str(s)
    s = " ".join(s.split())
    s = s.replace("@", "(at)").replace("`", "'")
    if len(s) > maxlen:
        s = s[: maxlen - 3] + "..."
    return s


def log(*args):
    print("[%s]" % time.strftime("%Y-%m-%dT%H:%M:%S"), *args, flush=True)


def _request(
    url, method="GET", headers=None, data=None, timeout=10, user_agent="bridge-common"
):
    """One HTTP call, JSON in and out. Always sends a User-Agent (Discord Cloudflare 1010-403s
    without one). Raises the underlying urllib error on failure — callers that need a
    descriptive `down` message catch and format it themselves."""
    hdrs = {"User-Agent": user_agent}
    if headers:
        hdrs.update(headers)
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, headers=hdrs, data=body, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (internal URLs)
        raw = resp.read()
        return json.loads(raw) if raw else None


def push(kuma_url, token, ok, msg, timeout=10, user_agent="bridge-common"):
    """POST a Kuma push-monitor heartbeat. Best-effort: a failed push is logged, never raised —
    one dead push token must not crash the loop reporting every other check.

    `user_agent` is threaded through so each bridge keeps the identity it sent before this
    module existed — monitor-bridge, not the shared module's name. Extracting shared code must
    not change what goes out on the wire.
    """
    if not token:
        log("WARN: no push token set; skipping push:", msg)
        return
    qs = urllib.parse.urlencode({"status": "up" if ok else "down", "msg": msg})
    try:
        _request(
            "%s/api/push/%s?%s" % (kuma_url, token, qs),
            timeout=timeout,
            user_agent=user_agent,
        )
    except Exception as e:  # best-effort heartbeat; never crash the loop
        log("push failed (%s):" % msg, e)


def touch_heartbeat(path):
    try:
        with open(path, "w") as fh:
            fh.write("%s\n" % time.time())
    except OSError as e:  # best-effort like push(); never crash the loop
        log("WARN: heartbeat write failed:", e)


def run_loop(once, interval, cycle, heartbeat):
    """The shared sleep loop: run one cycle, touch the heartbeat, sleep, repeat until --once.

    `cycle` and `heartbeat` are zero-arg callables so each script closes over its own config
    (the CHECKS registry / *arr streaks, HEARTBEAT_FILE) rather than this module knowing about
    either.
    """
    while True:
        cycle()
        heartbeat()
        if once:
            break
        time.sleep(interval)
