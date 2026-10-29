#!/usr/bin/env python3
"""Shared I/O-shell helpers for the host-run scripts.

Used by gitops_deploy.py, renovate_notify.py, janitorr_health.py, configarr_health.py,
longhorn_backup_health_logic.py, longhorn_reap_lib/logic.py, render_records.py, live_drift_check.py,
the triage poller and runner, deploy_ui.py, the fake-remux scripts and secret rotation's
rotation_tools.py. Each runs via
``uv run --no-project --python <pin>`` (host_python_version in
ansible/inventory/group_vars/all.yml) or directly under cron, and is deployed into its own
``/opt`` dir, where it does a ``sys.path.insert(0, <own dir>)`` so ``from host_lib import ...``
resolves the copy sitting alongside. Single source of truth for helpers that had drifted between
scripts: the Cloudflare-1010 User-Agent on the Discord POST and its length cap, the
torn-write-safe atomic state write, the config.env parser, the kubectl runner, the RFC3339
parser, the Uptime Kuma push, the GitHub token lookup and REST read, the journal-aware ``log``
with its ``logfmt`` fields and crash hook, and the node-exporter textfile write. Stdlib only.

The repository signer (homelab_repo_sign.py) imports it under the host's /usr/bin/python3,
Ubuntu 24.04's 3.12, so this file and kuma_push.py keep to the 3.12 grammar (#6202):
pyproject.toml formats both for py312, and tests/test_host_lib.py parses them with it.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Collection, Mapping

# The Kuma push lives in its own stdlib-only module so a pod can stage it without the rest of
# this file (#3745). Re-exported so every host caller keeps `from host_lib import kuma_push`.
from kuma_push import (  # noqa: F401
    KUMA_PUSH_ATTEMPTS,
    KUMA_PUSH_MSG_MAX,
    KUMA_PUSH_RETRY_DELAY_S,
    KUMA_PUSH_TIMEOUT_S,
    cap_kuma_msg,
    kuma_push,
)

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
# bound. A flush posts at most four, so a backlog drains over several calls instead of in one
# burst; a 429 from a burst only re-queues the message anyway.
DISCORD_SPOOL_MAX = 50
DISCORD_SPOOL_FLUSH_MAX = 4

GITHUB_API = "https://api.github.com"
GITHUB_TIMEOUT_S = 15

# sd-daemon(3) priorities for `log`'s `level`. Without one, journald files a line at the unit's
# `SyslogLevel=` (notice on the deployer), so `journalctl -p err` finds nothing for a failure
# (#4446). ERR marks a failure the run could not work around; WARNING a read or a gate that
# failed and the run worked around.
ERR = 3
WARNING = 4

# A logfmt value with any of these, or an empty one forced to quote, is written in quotes.
_LOGFMT_NEEDS_QUOTES = re.compile(r'[\s"=\\]')


def _fd_is_journal(fd: int) -> bool:
    """Whether `fd` is the journal stream systemd connected, per `JOURNAL_STREAM`.

    systemd sets `JOURNAL_STREAM=<dev>:<inode>` for the stream it hands a unit, and the
    variable is inherited by anything the unit starts. So matching it against `fd` itself,
    not just reading that it is set, is what keeps a test run under a systemd service from
    printing `<3>` into captured output.
    """
    try:
        dev, ino = os.environ["JOURNAL_STREAM"].split(":")
        st = os.fstat(fd)
        return (st.st_dev, st.st_ino) == (int(dev), int(ino))
    except (KeyError, ValueError, OSError):
        return False


def _stream_is_journal(stream) -> bool:
    try:
        return _fd_is_journal(stream.fileno())
    except (AttributeError, OSError, ValueError):  # pytest's capture has no fd
        return False


def logfmt(fields: Mapping[str, object], always_quote: Collection[str] = ()) -> str:
    """`fields` as space-separated logfmt `key=value` pairs, in the mapping's order.

    A value holding whitespace, `"`, `=` or `\\` is double-quoted with `"` and `\\` escaped,
    the form Loki's `| logfmt` parser reads back. Any other value is bare, an empty one
    included (`cause=`), and None is empty. A key in `always_quote` is quoted even when bare
    would parse, for a line whose readers already match the quoted form.
    """
    pairs = []
    for key, value in fields.items():
        text = "" if value is None else str(value)
        if key in always_quote or _LOGFMT_NEEDS_QUOTES.search(text):
            text = '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
        pairs.append(f"{key}={text}")
    return " ".join(pairs)


def deploy_event(
    services: str,
    sha: str,
    result: str = "ok",
    *,
    duration_s: float | None = None,
    source: str | None = None,
) -> str:
    """The `event=deploy` logfmt line Grafana draws as a deploy annotation.

    `result` is `ok`, `fail` or `rollback`. A `result=ok` line carries no `duration_s`, so it
    stays byte-identical to the line every emitter wrote before failures were recorded (#6164).
    `observability_deploy_annotation_expr` reads these lines from syslog, and flux_soak.py
    writes the same shape from a pod that cannot import this module.
    """
    fields: dict[str, object] = {
        "event": "deploy",
        "services": services,
        "sha": sha,
        "result": result,
    }
    if duration_s is not None:
        fields["duration_s"] = int(duration_s)
    if source is not None:
        fields["source"] = source
    return logfmt(fields)


def log(
    msg: str,
    level: int | None = None,
    *,
    tag: str = "",
    stream=None,
    **fields: object,
) -> None:
    """Print `msg`, prefixed `<tag>: ` and followed by `fields` as logfmt, and flush.

    `stream` defaults to stdout. With a `level`, each line carries the sd-daemon `<n>` prefix
    when `stream` is the journal: journald reads the prefix off every line it receives, and an
    exception interpolated into `msg` is often several lines. Off the journal the text is
    unchanged, so a terminal or a cron mail never shows `<3>`.
    """
    out = sys.stdout if stream is None else stream
    text = f"{tag}: {msg}" if tag else msg
    if fields:
        text = f"{text} {logfmt(fields)}"
    if level is not None and _stream_is_journal(out):
        text = "\n".join(f"<{level}>{line}" for line in text.split("\n"))
    print(text, file=out, flush=True)


def install_excepthook() -> None:
    """Make an uncaught exception's traceback reach the journal at err priority.

    A unit at `SyslogLevel=notice` files a re-raised traceback at notice, so `journalctl -p
    err` missed every crash; #4446 fixed only the failures the code handles. The hook writes
    the traceback through `log` on stderr, which prefixes `<3>` when stderr is the journal and
    leaves it byte for byte Python's own otherwise. A script calls this from `__main__`, never
    at import, so a test importing the module keeps the default hook.
    """

    def hook(exc_type, exc, tb) -> None:
        text = "".join(traceback.format_exception(exc_type, exc, tb)).rstrip("\n")
        log(text, ERR, stream=sys.stderr)

    sys.excepthook = hook


def write_textfile(
    path: str | os.PathLike[str], text: str | bytes, mode: int = 0o644
) -> None:
    """Replace `path` with `text` atomically, at `mode`, for a reader polling the directory.

    The temp file is `mkstemp`'s, in the same directory, named `.<basename>.<random>`, so it
    never ends in `.prom` and the node-exporter textfile collector skips a half-written file.
    `mkstemp` creates 0600 and the collector reads as uid 65534, hence the explicit `mode`.
    A failed write removes the temp file and re-raises.
    """
    directory, name = os.path.split(path)
    fd, tmp = tempfile.mkstemp(dir=directory or ".", prefix=f".{name}.")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(text.encode() if isinstance(text, str) else text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


_RFC3339_FMT = "%Y-%m-%dT%H:%M:%SZ"
_FRACTIONAL_SECONDS_RE = re.compile(r"\.\d+")


def rfc3339_to_epoch(ts: str) -> float | None:
    """Seconds since the epoch for an RFC3339 timestamp, or None if unparseable.

    Was duplicated between longhorn_backup_health_logic.py and longhorn_reap_lib/logic.py, and
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


def flush_discord_spool(
    spool_dir: str, webhook: str, user_agent: str, log=None
) -> bool:
    """Post up to ``DISCORD_SPOOL_FLUSH_MAX`` messages queued in ``spool_dir``, oldest first.

    A ``discord_post(spool_dir=...)`` caller runs this once per run, news or not, so a queued
    post goes out within one run of the network returning. An absent spool costs one listing
    and no request. False when the webhook is empty, or a delivery failed for a reason another
    attempt can fix (no network, a 5xx, a 429); that message and the rest stay queued.
    """
    if not webhook:
        return False
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

    ``spool_dir`` (optional) is for a caller that never retries a post itself. A post that
    fails for a reason another attempt can fix (no network, a 5xx, a 429) is queued there, and
    ``flush_discord_spool`` later sends it with a ``(delayed: first attempt <UTC time>)`` line.
    This call runs that flush first; while the flush fails, the new post joins the queue without
    an attempt of its own. Such a caller also runs the flush once per run (#3905). A caller
    that gates a marker on the return value must NOT pass it: its next run re-sends the post,
    so it would arrive twice. The return value is unchanged.
    """
    if not webhook:
        if log:
            log("no Discord webhook set; skipping post")
        return False
    message = f"{marker} {content}" if marker else content
    if spool_dir and not flush_discord_spool(spool_dir, webhook, user_agent, log):
        _spool_message(spool_dir, message, log)
        return False
    status = _discord_send(webhook, clamp_discord(message), user_agent, log)
    if status is not None and 200 <= status < 300:
        return True
    if spool_dir and not _discord_rejected(status):
        _spool_message(spool_dir, message, log)
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
    during a landing, renovate_notify, the ruleset-drift cron. ``await_ci.py`` polls every 20s
    for up to land.sh's 1200s budget, which would be 61 polls of 2 or more requests each. When
    this returns None it polls every 90s and skips its descendant walk, which bounds a wait at
    30 requests (``await_ci.ANONYMOUS_INTERVAL_S``, #6148). Before that back-off, two
    ``land.sh`` runs in an hour exhausted it, after which the tick's
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
        except (subprocess.SubprocessError, OSError):
            return None
        if proc.returncode != 0:
            return None
        return [line for line in proc.stdout.splitlines() if line.strip()]

    return journal
