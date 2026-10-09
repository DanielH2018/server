#!/usr/bin/env python3
"""Shared I/O-shell helpers for the host-run scripts.

Used by gitops_deploy.py, renovate_notify.py, janitorr_health.py, configarr_health.py,
longhorn_backup_health_logic.py, longhorn_reap_logic.py, render_records.py, live_drift_check.py
and secret rotation's rotation_tools.py. Each runs via
``uv run --no-project --python <pin>`` (host_python_version in
ansible/inventory/group_vars/all.yml) or directly under cron, and is deployed into its own
``/opt`` dir, where it does a ``sys.path.insert(0, <own dir>)`` so ``from host_lib import ...``
resolves the copy sitting alongside. Single source of truth for helpers that had drifted between
scripts: the Cloudflare-1010 User-Agent on the Discord POST and its length cap, the
torn-write-safe atomic state write, the config.env parser, the kubectl runner, the RFC3339
parser, the Uptime Kuma push, and the GitHub token lookup and REST read below. Stdlib only.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping

# Cron inherits neither a useful PATH nor KUBECONFIG. `k3s` and `kubectl` both live in
# /usr/local/bin, which the cron default omits, so a caller that does not fix this gets an
# OSError that reads like a missing binary rather than a missing PATH.
LOCAL_BIN = "/usr/local/bin"

# Distinct from any exit code kubectl itself returns, so a caller can tell "the cluster said no"
# from "we never reached the cluster".
KUBECTL_TIMEOUT_RC = 124
KUBECTL_UNRUNNABLE_RC = 125

# Discord rejects a message over 2000 characters with HTTP 400. This is the one cap every host
# poster shares (#3351). monitor-bridge's bridge/common.py carries the pod-side twin of this cap
# and `clamp_discord`, because the two programs ship by different mechanisms and cannot share a
# module, so edit both together.
DISCORD_MAX = 1900
DISCORD_TRUNCATED = "\n…(truncated)"

# discord_post's opt-in spool (#3905). A daily script spools one message a day, so 50 holds an
# outage far longer than the three days of #3882, and a revoked webhook cannot grow it without
# bound. Four flushed plus the new post is five, the size of Discord's per-webhook burst, so a
# flush after an outage does not 429 itself.
DISCORD_SPOOL_MAX = 50
DISCORD_SPOOL_FLUSH_MAX = 4

# kuma-push-lib.sh's contract for the shell crons. That file's header has the measurements
# behind each number (#1010, #2013). 900 keeps the msg inside Discord's 1024-character embed
# field once Kuma adds its own text.
KUMA_PUSH_MSG_MAX = 900
KUMA_PUSH_ATTEMPTS = 3
KUMA_PUSH_RETRY_DELAY_S = 30
KUMA_PUSH_TIMEOUT_S = 10

GITHUB_API = "https://api.github.com"
GITHUB_TIMEOUT_S = 15

_RFC3339_FMT = "%Y-%m-%dT%H:%M:%SZ"
_FRACTIONAL_SECONDS_RE = re.compile(r"\.\d+")


def rfc3339_to_epoch(ts: str) -> float | None:
    """Seconds since the epoch for an RFC3339 timestamp, or None if unparseable.

    Was duplicated between longhorn_backup_health_logic.py and longhorn_reap_logic.py, and
    already diverged once: only the backup-health copy stripped fractional seconds, so the same
    Longhorn `snapshotCreatedAt` parsed in one module and returned None in the other (2026-09-04
    review). This is the single copy both now call.

    Mirrors `date -d "$ts" +%s 2>/dev/null` returning empty on failure — permissively, not
    strictly: `.status.snapshotCreatedAt` is a plain string Longhorn writes itself with no format
    guarantee, while `.metadata.creationTimestamp` is a Kubernetes metav1.Time (always
    seconds-precision UTC with a trailing Z). `date -d` accepts fractional seconds and a numeric
    UTC offset for either, so this must too, or a sub-second stamp reads as unparseable and pages
    a false RED on every tick. Fractional digits are dropped (bash's `date -d` truncates them
    too, and every caller here compares at whole-second resolution) before falling back to the
    strict seconds-only path for plain `...Z` input.

    Callers that need bash's `|| echo 0` sentinel behaviour (an epoch of exactly 0 treated as
    unparseable, since no real Longhorn object carries a 1970-01-01 timestamp) apply that on top
    of this — it is not baked in here, because not every caller wants it.
    """
    if not ts:
        return None
    normalized = _FRACTIONAL_SECONDS_RE.sub("", ts)
    try:
        return _dt.datetime.fromisoformat(normalized.replace("Z", "+00:00")).timestamp()
    except ValueError:
        pass
    try:
        return (
            _dt.datetime.strptime(normalized, _RFC3339_FMT)
            .replace(tzinfo=_dt.timezone.utc)
            .timestamp()
        )
    except ValueError:
        return None


def parse_env_file(path: str) -> dict[str, str]:
    """Parse a ``KEY=VALUE`` ``config.env`` file into a dict.

    Skips blank lines and ``#`` comments, and splits on the first ``=`` so a value may itself
    contain ``=``.
    """
    out: dict[str, str] = {}
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k] = v
    return out


def atomic_write(path: str, text: str) -> None:
    """Write ``text`` to ``path`` atomically via a temp file plus ``os.replace``.

    A concurrent reader never sees a half-written file. monitor-bridge reads these marker/state
    files every 300s with no retry and ``float()``s an empty read into a false "unparseable"
    DOWN page — the torn-write class 58056d18 closed for the shell state writers, applied to
    the Python twins.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(text)
    os.replace(tmp, path)


def clamp_discord(content: str, limit: int = DISCORD_MAX) -> str:
    """``content`` verbatim when it fits ``limit``, else cut so it ENDS in the truncation marker.

    The marker is the reader's only way to tell a cut message from a complete one. Two caps
    used to apply in sequence: renovate_notify clamped to 1950 with this marker, then
    discord_post cut to 1900 and sliced the marker off every clamped digest (#3351).
    """
    if len(content) <= limit:
        return content
    return content[: limit - len(DISCORD_TRUNCATED)].rstrip() + DISCORD_TRUNCATED


def _discord_send(webhook: str, content: str, user_agent: str, log=None) -> int | None:
    """POST one already-clamped message; the HTTP status, or None when nothing answered."""
    # The Cloudflare-1010 rationale in discord_post's docstring is duplicated in monitor-bridge's
    # bridge/net.py `_get_json`, which sets the same header for its Discord webhook GETs. The two
    # programs ship by different mechanisms and cannot share a module, so edit both together.
    data = json.dumps({"content": content}).encode()
    req = urllib.request.Request(
        webhook,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": user_agent},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        e.close()  # the error carries the open response
        if log:
            log("discord post failed: %s" % e)
        return e.code
    except Exception as e:  # alerting must never crash the caller
        if log:
            log("discord post failed: %s" % e)
        return None


def _discord_rejected(status: int | None) -> bool:
    """True for an answer another attempt cannot change: a 4xx other than a 429.

    A 400 is a payload Discord refuses and a 401/404 a revoked webhook. A spooled message that
    gets one is dropped rather than kept, or it would block every message queued behind it.
    """
    return status is not None and 400 <= status < 500 and status != 429


def _spool_message(spool_dir: str, message: str, log=None) -> None:
    """Queue ``message`` for a later post, keeping at most ``DISCORD_SPOOL_MAX`` files."""
    entry = {"queued_at": time.time(), "content": message}
    # Nanoseconds first, so a name sort is a queue-order sort; the pid separates two scripts
    # sharing one spool.
    name = "%d-%d.json" % (time.time_ns(), os.getpid())
    try:
        atomic_write(os.path.join(spool_dir, name), json.dumps(entry))
        queued = sorted(n for n in os.listdir(spool_dir) if n.endswith(".json"))
        for stale in queued[: max(0, len(queued) - DISCORD_SPOOL_MAX)]:
            os.remove(os.path.join(spool_dir, stale))
            if log:
                log("discord spool full; dropped %s" % stale)
    except OSError as e:
        if log:
            log("discord spool write failed: %s" % e)
        return
    if log:
        log("discord post queued in %s" % spool_dir)


def _delayed_note(queued_at: float) -> str:
    when = _dt.datetime.fromtimestamp(queued_at, _dt.timezone.utc)
    return "\n(delayed: first attempt %s)" % when.strftime("%Y-%m-%d %H:%M UTC")


def _flush_spool(spool_dir: str, webhook: str, user_agent: str, log=None) -> bool:
    """Post up to ``DISCORD_SPOOL_FLUSH_MAX`` queued messages, oldest first.

    False when a delivery failed for a reason another attempt can fix, such as no network, a
    5xx or a 429. That message and the ones after it stay queued.
    """
    try:
        queued = sorted(n for n in os.listdir(spool_dir) if n.endswith(".json"))
    except OSError:
        return True  # nothing has been queued yet
    for name in queued[:DISCORD_SPOOL_FLUSH_MAX]:
        path = os.path.join(spool_dir, name)
        try:
            with open(path) as fh:
                entry = json.load(fh)
            message, queued_at = str(entry["content"]), float(entry["queued_at"])
        except FileNotFoundError:
            continue  # another script sharing the spool already delivered it
        except (OSError, ValueError, KeyError, TypeError) as e:
            if log:
                log("discord spool: dropping unreadable %s: %s" % (name, e))
            _remove_quietly(path)
            continue
        note = _delayed_note(queued_at)
        status = _discord_send(
            webhook,
            clamp_discord(message, DISCORD_MAX - len(note)) + note,
            user_agent,
            log,
        )
        if status is not None and 200 <= status < 300:
            _remove_quietly(path)
        elif _discord_rejected(status):
            if log:
                log(
                    "discord spool: Discord rejected %s with %s; dropped"
                    % (name, status)
                )
            _remove_quietly(path)
        else:
            return False
    return True


def _remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def discord_post(
    webhook: str,
    content: str,
    user_agent: str,
    log=None,
    marker: str = "",
    spool_dir: str | None = None,
) -> bool:
    """POST ``content`` to a Discord ``webhook``.

    Returns True ONLY on a confirmed 2xx, so a caller can gate a per-SHA dedupe marker/fingerprint
    on it — a transient failure returning True would advance the marker and permanently suppress
    that alert. A ``user_agent`` is REQUIRED: Discord is behind Cloudflare, which 403s the default
    python-urllib UA (error 1010). An empty webhook or any error returns False (so the caller
    retries next run) and never raises, so alerting can't crash the caller. ``log`` (optional
    callable) is called with a one-line reason on skip/failure.

    ``marker`` (optional) is prepended to the posted message so the automation's output is
    self-identifying in a shared channel — the ``user_agent`` is a header-only marker Discord never
    renders. Every automation's Discord message should carry a stable ``<automation>:`` identifier,
    either via this arg or baked into ``content`` (as gitops_deploy / renovate_notify already do).

    The posted message, ``marker`` included, goes through ``clamp_discord``, so an over-long
    message arrives cut to ``DISCORD_MAX`` and ending in the truncation marker.

    ``spool_dir`` (optional) is for a caller that never retries a message itself. Such a caller
    ignores the return value, so before #3905 a post sent while the host could not reach Discord
    was lost. With ``spool_dir`` set, a post that fails for a reason another attempt can fix
    (no network, a 5xx, a 429) is queued as a file there. The next call with the same
    ``spool_dir`` first posts what is queued, oldest first, each ending in a ``(delayed: first
    attempt <UTC time>)`` line. While the queue cannot be delivered, a new message joins it
    without its own attempt. A caller that gates a marker on the return value must NOT pass
    ``spool_dir``, since its next run re-sends the message and the spool would post it twice.
    The return value is unchanged: False means "not delivered yet", queued or not.
    """
    if not webhook:
        if log:
            log("no Discord webhook set; skipping post")
        return False
    message = f"{marker} {content}" if marker else content
    if spool_dir and not _flush_spool(spool_dir, webhook, user_agent, log):
        _spool_message(spool_dir, message, log)
        return False
    status = _discord_send(webhook, clamp_discord(message), user_agent, log)
    if status is not None and 200 <= status < 300:
        return True
    if spool_dir and not _discord_rejected(status):
        _spool_message(spool_dir, message, log)
    return False


def cap_kuma_msg(msg: str, limit: int = KUMA_PUSH_MSG_MAX) -> str:
    """``msg`` verbatim when it fits ``limit``, else cut with a `` …(+N chars)`` marker.

    The same cut as kuma-push-lib.sh's ``msg_max`` arm and monitor-bridge's ``cap_push_msg``.
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
        host: the Kuma hostname. Empty skips the push.
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
    url = "https://%s/api/push/%s?%s" % (host, token, query)
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


def github_token(source: Mapping[str, str], run: Callable) -> str | None:
    """A GitHub token for REST reads, or None to query anonymously.

    ``GH_TOKEN`` then ``GITHUB_TOKEN`` in ``source`` win, then ``gh auth token``. ``source`` is
    wherever the caller keeps its settings: the deployer passes ``os.environ``, renovate_notify
    its ``config.env``. The gh CLI on daniel-box is logged in as the repo owner, and both callers
    run as that user. The lookup is best-effort: a missing gh, an expired login, or a slow
    keyring all return None, and the caller queries anonymously.

    Why authenticate a read of a public repo. The anonymous limit is 60 requests/hour PER
    SOURCE IP, and every GitHub call from this host shares it: the tick's gate, ``await_ci.py``
    polling every 20s for up to 900s during a landing (45 requests per run), renovate_notify,
    the ruleset-drift cron. Two ``land.sh`` runs in an hour exhaust it, after which the tick's
    gate reads ``HTTP Error 403: rate limit exceeded`` and defers as ``CI not finished``. That
    is correct fail-closed behaviour, and also a deploy outage nobody asked for. Measured
    2026-09-01: two landings and a manual tick, three 403 deferrals. Authenticated, the limit
    is 5000/hour per token. This was two copies, in deploy_git and renovate_notify, that
    disagreed on which variables they read (#3362).
    """
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        value = (source.get(name) or "").strip()
        if value:
            return value
    try:
        proc = run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10)
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return (proc.stdout or "").strip() or None


def github_auth_headers(token: str | None) -> dict[str, str]:
    """The ``Authorization`` header for ``token``, or nothing for an anonymous request."""
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


def github_get(
    path: str,
    token: str | None,
    *,
    user_agent: str,
    timeout: float = GITHUB_TIMEOUT_S,
    opener: Callable | None = None,
):
    """GET ``https://api.github.com/<path>`` and return the parsed JSON body.

    The one request shape every host GitHub reader shares, so the readers cannot drift on the
    headers, the auth or the timeout (#2136, #3362). What a failure MEANS stays with the caller:
    the deployer's gate maps every error to ``pending``, a landing lets it raise.

    Args:
        path: the part after the API root, query string included, e.g. ``repos/o/n/pulls``.
        token: from ``github_token``, looked up once by the caller; None reads anonymously.
        user_agent: names the caller in GitHub's logs. GitHub refuses a request without one.
        timeout: seconds for the whole request.
        opener: the ``urlopen`` that carries the request, ``urllib.request.urlopen`` when None,
            resolved per call so a stub of the stdlib name reaches it too.

    Raises:
        urllib.error.URLError: the API could not be reached, or answered non-2xx
            (``HTTPError`` is a subclass).
        TimeoutError, OSError: the socket failed.
        ValueError: the body was not JSON.
    """
    req = urllib.request.Request(
        "%s/%s" % (GITHUB_API, path.lstrip("/")),
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": user_agent,
            **github_auth_headers(token),
        },
    )
    with (opener or urllib.request.urlopen)(req, timeout=timeout) as resp:
        return json.load(resp)


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
        """Run kubectl with the bound namespace and args, returning (rc, output)."""
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


def file_mtime(path):
    """(mtime, unreadable) for `path` — (None, False) when it simply does not exist.

    `unreadable` is True only when the path EXISTS but could not be stat'd, which on these
    hosts means a permissions change. A caller that folded the two together would report "the
    file is gone" for a file it merely could not look at, and those have different fixes — the
    lesson longhorn_backup_health._read_stamp records from the 2026-08-19 restore-drill
    incident. The Longhorn heartbeat's check 10 reads a cron file's install time this way.
    """
    try:
        return os.stat(path).st_mtime, False
    except FileNotFoundError:
        return None, False
    except OSError:
        return None, True


def journal_reader(binary, window_hours, timeout):
    """Return a `journal(tag) -> list or None` bound to one binary, window and timeout.

    Reads the messages a syslog tag wrote in the last `window_hours`. A host cron that reports
    through `logger` is only evidence once something reads it back, and the caller that needs
    that is the Longhorn backup-plane heartbeat (check 9, #2418). Bound the same way
    kubectl_runner is, for the same reason: the binary path, the window and the deadline are the
    caller's settings, and the subprocess handling should not be copied per caller.

    `--output=cat` prints the message alone, so each returned line is exactly one journal entry
    and `logger`'s one-entry-per-input-line behaviour is preserved. That drops the timestamps,
    which is why the window is bounded by `--since` rather than by parsing dates back out.

    Returns None when the read itself failed — a nonzero rc, a timeout, or a binary that would
    not spawn. An empty list is a different answer and means the window held no message. A
    caller that folded the two together would report a broken journalctl as a quiet cron.
    """
    argv = binary.split()

    def journal(tag):
        """Messages `tag` logged inside the window, newest last, or None if the read failed."""
        try:
            proc = subprocess.run(
                [
                    *argv,
                    "-t",
                    tag,
                    "--since",
                    "-%sh" % window_hours,
                    "--output=cat",
                    "--no-pager",
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.SubprocessError, OSError:
            return None
        if proc.returncode != 0:
            return None
        return [line for line in proc.stdout.splitlines() if line.strip()]

    return journal
