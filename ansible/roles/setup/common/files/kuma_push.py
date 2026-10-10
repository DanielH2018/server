#!/usr/bin/env python3
"""The Uptime Kuma push, shared by every Python pusher on a host and in a pod (#3745).

Stdlib only, and importing nothing first-party, so a pod can stage this one file through its
ConfigMap the way autofix-bridge stages monitor-bridge's ``bridge/common.py``. ``host_lib``
re-exports it for the host scripts, and ``tasks/install_host_lib.yml`` copies both files beside
each of them. The retry and the message cap stay behind ``kuma_push``, so no caller restates
either.

kuma-push-lib.sh is the shell twin of the same contract, a separate implementation for the
shell crons. Must import on the host Python floor (``test_host_python_pin.py``).
"""

from __future__ import annotations

import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

# kuma-push-lib.sh's contract for the shell crons. That file's header has the measurements
# behind each number (#1010, #2013). 900 keeps the msg inside Discord's 1024-character embed
# field once Kuma adds its own text.
KUMA_PUSH_MSG_MAX = 900
KUMA_PUSH_ATTEMPTS = 3
KUMA_PUSH_RETRY_DELAY_S = 30
KUMA_PUSH_TIMEOUT_S = 10


def cap_kuma_msg(msg: str, limit: int = KUMA_PUSH_MSG_MAX) -> str:
    """``msg`` verbatim when it fits ``limit``, else cut with a `` …(+N chars)`` marker.

    The same cut as kuma-push-lib.sh's ``msg_max`` arm and monitor-bridge's ``cap_push_msg``,
    which keeps a trailing `` (N cycles)`` streak suffix this cut does not.
    Kuma copies the msg into a Discord embed field and never truncates it, so an oversized msg
    gets the whole DOWN alert rejected (#2013).
    """
    if len(msg) <= limit:
        return msg
    # The marker's own width depends on the count it carries, so settle it in two passes.
    dropped = len(msg)
    for _ in range(2):
        keep = max(0, limit - len(" …(+%d chars)" % dropped))
        dropped = len(msg) - keep
    return msg[:keep] + " …(+%d chars)" % dropped


def kuma_push(
    status: str,
    msg: str,
    host: str,
    token: str,
    *,
    log: Callable[[str], None] | None = None,
    opener: Callable | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Push ``status`` (``up`` or ``down``) and ``msg`` to the Kuma push monitor for ``token``.

    The Python twin of kuma-push-lib.sh's ``kuma_push``, with the same retry rule: three
    attempts 30s apart, stopping early only on a 401 or 403, which a retry cannot fix. Every
    other failure retries, a 404 above all, because Traefik drops the push route while
    uptime-kuma rolls out and answers 404 for the whole window (#1010). ``msg`` is capped by
    ``cap_kuma_msg`` first.

    DECIDED: this resolves ``host`` through DNS, where the shell library pins Traefik's VIP with
    ``curl --resolve``. urllib cannot express that pin (scripts/diagnostics/probe_lib/obs_api.py
    records the same limit). Shelling out to curl would make every importer depend on a binary
    for a failure mode the retry already covers, since a resolver outage that outlasts 90s of
    retries also takes down the other signals the tile's reader has. The token stays out of
    argv either way, because urllib starts no process.

    Args:
        status: Kuma's own vocabulary, ``up`` or ``down``.
        msg: the message Kuma shows on the tile and in its alert.
        host: the Kuma hostname, pushed to over https through Traefik, or a base URL with its
            scheme (``http://uptime-kuma.monitoring:3001``), which a pod uses to reach the
            in-cluster Service directly. Empty skips the push.
        token: the push monitor's token. Empty skips the push.
        log: called with one line per failed attempt. It never receives the URL, which
            carries the token.
        opener: the ``urlopen`` that carries the push, ``urllib.request.urlopen`` when None,
            resolved per call so a stub of the stdlib name reaches it too.
        sleep: the wait between attempts; a test injects a no-op.

    Returns:
        True only when Kuma answered 2xx. Never raises: a lost push leaves the tile to expire
        at its interval, and must not turn the caller's verdict into a crash.
    """
    if not host or not token:
        if log:
            log("no Kuma host/token set; not pushing (status=%s: %s)" % (status, msg))
        return False
    msg = cap_kuma_msg(msg)
    query = urllib.parse.urlencode({"status": status, "msg": msg, "ping": ""})
    base = host.rstrip("/") if "://" in host else "https://" + host
    url = "%s/api/push/%s?%s" % (base, token, query)
    reason = ""
    for attempt in range(1, KUMA_PUSH_ATTEMPTS + 1):
        try:
            with (opener or urllib.request.urlopen)(
                url, timeout=KUMA_PUSH_TIMEOUT_S
            ) as resp:
                resp.read()
            return True
        except urllib.error.HTTPError as e:
            # A JSON body means Kuma answered: the edge routed the push and the token has no
            # live monitor. Traefik's no-router 404 is text/plain. The shell twin logs the same
            # `by=kuma` field (#1803).
            ctype = e.headers.get_content_type() if e.headers else ""
            by = " by=kuma" if ctype == "application/json" else ""
            reason = "http=%s%s" % (e.code, by)
            e.close()
            if e.code in (401, 403):
                break
        except (
            Exception
        ) as e:  # any transport failure; a push must never crash the caller
            reason = "error=%s" % type(e).__name__
        if attempt < KUMA_PUSH_ATTEMPTS:
            if log:
                log(
                    "push failed transiently (%s) (status=%s: %s), retrying in %ss"
                    % (reason, status, msg, KUMA_PUSH_RETRY_DELAY_S)
                )
            sleep(KUMA_PUSH_RETRY_DELAY_S)
    if log:
        log("push failed (%s) (status=%s: %s)" % (reason, status, msg))
    return False
