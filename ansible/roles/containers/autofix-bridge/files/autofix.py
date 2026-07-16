#!/usr/bin/env python3
"""autofix-bridge — auto-remediation sidecar for Sonarr/Radarr (queue-blocklist + fake-remux modules).

The mutating twin of the read-only monitor-bridge. Each cycle it polls Sonarr's and Radarr's
own /api/v3/queue, classifies items as auto-block candidates (the narrow hard-bad +
malware-signature classes), tracks a consecutive-cycle streak in-process for a grace period,
caps the per-cycle blast radius, then — unless DRY_RUN — DELETEs the item with blocklist=true
(removes from client + blocklists the release) and fires a series/movie re-search so the *arr
grabs a clean replacement. Health -> its own Uptime Kuma push monitor; each action -> the *arr
Discord webhook. Stdlib only (python:3.14-alpine); config is env-driven so this stays testable.

A second, slower module (run_fake_remux_scan) scans the Sonarr library daily for "fake remuxes" —
files whose quality claims a <=1080p Remux but whose stream is HEVC (a re-encode mislabeled as a
remux). It has its OWN dry-run (FAKEREMUX_DRY_RUN, default true) because it DELETES library files +
re-searches; the queue module's DRY_RUN does not gate it.

Design: docs/superpowers/specs/2026-07-06-autofix-bridge-disk-autoprune-design.md (Part A)
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request


def _env(name, default):
    return os.environ.get(name, default)


INTERVAL = int(_env("INTERVAL", "300"))
HTTP_TIMEOUT = int(_env("HTTP_TIMEOUT", "10"))
HEARTBEAT_FILE = _env("HEARTBEAT_FILE", "/tmp/heartbeat")
KUMA_URL = _env("KUMA_URL", "http://uptime-kuma:3001").rstrip("/")
KUMA_PUSH = _env("KUMA_PUSH_ARR_AUTOBLOCK", "")

SONARR_URL = _env("SONARR_URL", "http://sonarr:8989").rstrip("/")
SONARR_API_KEY = _env("SONARR_API_KEY", "")
RADARR_URL = _env("RADARR_URL", "http://radarr:7878").rstrip("/")
RADARR_API_KEY = _env("RADARR_API_KEY", "")


def _dry_run_enabled(val):
    """Fail-safe dry-run: enabled UNLESS explicitly disabled (0/false/no)."""
    return str(val).strip().lower() not in ("0", "false", "no")


DISCORD_WEBHOOK_URL = _env("ARR_DISCORD_WEBHOOK_URL", "")
DRY_RUN = _dry_run_enabled(_env("DRY_RUN", "true"))
GRACE_CYCLES = int(_env("GRACE_CYCLES", "3"))
MAX_ACTIONS_PER_CYCLE = int(_env("MAX_ACTIONS_PER_CYCLE", "5"))
DANGEROUS_MSG_PATTERNS = [
    p.strip().lower()
    for p in _env(
        "DANGEROUS_MSG_PATTERNS",
        "executable file with extension,potentially dangerous,sample",
    ).split(",")
    if p.strip()
]
CLIENT_ERROR_PATTERNS = [
    p.strip().lower()
    for p in _env(
        "CLIENT_ERROR_PATTERNS",
        "unable to communicate,not responding,failed to connect,"
        "connection refused,download client is unavailable",
    ).split(",")
    if p.strip()
]

HARD_BAD_STATUS = frozenset({"error"})
HARD_BAD_STATE = frozenset({"importBlocked", "importFailed"})

# --- fake-remux scan (second module) -----------------------------------------
# Its OWN dry-run, independent of the queue check's DRY_RUN and defaulting to true: this module
# DELETES imported library files, so it stays report-only until deliberately flipped live.
FAKEREMUX_DRY_RUN = _dry_run_enabled(_env("FAKEREMUX_DRY_RUN", "true"))
FAKEREMUX_SCAN_INTERVAL = int(_env("FAKEREMUX_SCAN_HOURS", "24")) * 3600
FAKEREMUX_MAX_PER_SCAN = int(_env("FAKEREMUX_MAX_PER_SCAN", "5"))
FAKE_REMUX_CODECS = frozenset({"h265", "hevc", "x265"})


def sanitize(s, maxlen=120):
    """Neutralize adversary-controlled text (release titles, statusMessages) before it enters
    a Discord-bound string: collapse whitespace, defuse @mentions/backticks, cap length."""
    s = "?" if s is None else str(s)
    s = " ".join(s.split())
    s = s.replace("@", "(at)").replace("`", "'")
    if len(s) > maxlen:
        s = s[: maxlen - 3] + "..."
    return s


# --- pure decision core ------------------------------------------------------
def item_messages(item):
    """All statusMessage strings for a queue item, flattened."""
    out = []
    for sm in item.get("statusMessages") or []:
        out.extend(sm.get("messages") or [])
    return out


def dangerous(messages, patterns):
    """True if any statusMessage matches a known-dangerous substring (case-insensitive)."""
    low = [m.lower() for m in messages]
    pats = [p.lower() for p in patterns]
    return any(p in m for p in pats for m in low)


def all_error_text(item):
    """statusMessage strings PLUS the queue record's top-level errorMessage — download-client
    communication errors often land in errorMessage rather than statusMessages."""
    out = item_messages(item)
    err = item.get("errorMessage")
    if err:
        out = out + [err]
    return out


def client_comm_error(item, patterns):
    """True if the item's error text matches a transient download-client-communication problem
    (client unreachable / not responding), as opposed to a bad release. Reuses the same
    case-insensitive substring matcher as dangerous()."""
    return dangerous(all_error_text(item), patterns)


def is_candidate(item, patterns, client_error_patterns=()):
    """A queue item is an auto-block candidate when it is hard-bad OR malware-signature,
    EXCEPT a bare trackedDownloadStatus=='error' that looks like a transient download-client
    communication problem (client/VPN unreachable) — blocklisting that would wrongly nuke a
    legitimate in-progress download.

    - malware-signature (warning + dangerous statusMessage) -> candidate.
    - import-step failure (trackedDownloadState in importBlocked/importFailed) -> candidate
      (the download completed; a client outage can't produce these, so no exclusion).
    - bare error (trackedDownloadStatus=='error') -> candidate UNLESS client_comm_error matches.
    - everything else (transient warning, plain importPending) -> not a candidate (fails SAFE).
    """
    status = item.get("trackedDownloadStatus")
    state = item.get("trackedDownloadState")
    if status == "warning" and dangerous(item_messages(item), patterns):
        return True
    if state in HARD_BAD_STATE:
        return True
    if status in HARD_BAD_STATUS:
        return not client_comm_error(item, client_error_patterns)
    return False


def item_key(app_name, item):
    """Stable identity across cycles: the download-client hash, falling back to the queue id.

    App-scoped (prefixed with `app_name`) because Sonarr and Radarr number their queue `id`s
    independently — without the prefix, two unrelated items (one per app) that both lack
    `downloadId` would collide on the same fallback key and share a streak.

    Branch-tagged (`dl:`/`id:`) because a `downloadId` and a queue `id` share the same value
    space (a string hash today, but nothing stops a numeric-string `downloadId` from a future
    Usenet client like NZBGet from colliding with another item's integer `id` fallback) —
    the tag keeps the two branches from ever aliasing to the same key.
    """
    dl = item.get("downloadId")
    if dl:
        return "%s:dl:%s" % (app_name, dl)
    return "%s:id:%s" % (app_name, item.get("id"))


def item_reason(item):
    """Human reason for the action log: the statusMessages, else the status/state."""
    msgs = item_messages(item)
    return (
        "; ".join(msgs)
        or item.get("trackedDownloadStatus")
        or item.get("trackedDownloadState")
        or "warning"
    )


def eligible(candidate_keys, streaks, grace, max_actions):
    """Pure grace + blast-radius decision. MUTATES `streaks` in place.

    - increments the streak of each key that is a candidate THIS cycle;
    - drops keys that are no longer candidates (streak resets to 0);
    - a key is grace-met once its streak >= grace;
    Returns (to_act, held): the sorted grace-met keys to act on, OR — when more than
    max_actions are grace-met at once (a systemic mass-flag) — ([], all grace-met keys),
    so the loop acts on NONE and alerts instead.
    """
    for k in list(streaks):
        if k not in candidate_keys:
            del streaks[k]
    for k in candidate_keys:
        streaks[k] = streaks.get(k, 0) + 1
    met = sorted(k for k, n in streaks.items() if n >= grace)
    if len(met) > max_actions:
        return [], met
    return met, []


def search_command(app_name, item):
    """The /api/v3/command body to re-search the series/movie of a queue item, or None.

    Series/movie granularity (robust for season packs — a queue record's episodeId may not
    represent every episode in a stuck pack; the *arr only grabs genuine gaps).
    """
    if app_name == "Sonarr":
        sid = item.get("seriesId")
        if sid:
            return {"name": "SeriesSearch", "seriesId": sid}
    elif app_name == "Radarr":
        mid = item.get("movieId")
        if mid:
            return {"name": "MoviesSearch", "movieIds": [mid]}
    return None


def format_action(dry_run, app_name, title, reason, streak, grace):
    verb = "WOULD blocklist" if dry_run else "Blocklisted + re-searched"
    return "%s [%s] %s — %s (%d/%d)" % (
        verb,
        app_name,
        sanitize(title),
        sanitize(reason),
        streak,
        grace,
    )


def resolution_height(resolution):
    """Pixel height from a MediaInfo resolution like '1920x1080' -> 1080; None if unparsable."""
    if not resolution or "x" not in resolution:
        return None
    try:
        return int(resolution.rsplit("x", 1)[1])
    except ValueError:
        return None


def is_fake_remux(quality_name, resolution, video_codec):
    """A file whose quality claims a <=1080p Remux but whose stream is HEVC is a re-encode
    mislabeled as a remux: a real 720p/1080p Blu-ray remux is the untouched AVC (h264) disc
    stream, never HEVC. 2160p remuxes ARE legitimately HEVC, so the resolution gate excludes them,
    and an unknown resolution fails safe (not flagged). A definitive codec/quality contradiction —
    the NTRX 'BD Remux 1080p AVC' that actually shipped HEVC 10-bit (2026-07-16)."""
    if "remux" not in (quality_name or "").lower():
        return False
    height = resolution_height(resolution)
    if height is None or height > 1080:
        return False
    return (video_codec or "").strip().lower() in FAKE_REMUX_CODECS


def fake_files(episodefiles, series_title):
    """The fake-remux entries in one series' /api/v3/episodefile list, each flattened to the
    fields the delete + re-search + report need."""
    out = []
    for ef in episodefiles:
        mi = ef.get("mediaInfo") or {}
        quality = ((ef.get("quality") or {}).get("quality") or {}).get("name")
        if is_fake_remux(quality, mi.get("resolution"), mi.get("videoCodec")):
            out.append(
                {
                    "fileId": ef.get("id"),
                    "seriesId": ef.get("seriesId"),
                    "seriesTitle": series_title,
                    "path": ef.get("relativePath") or "?",
                    "quality": quality,
                    "codec": mi.get("videoCodec"),
                }
            )
    return out


# --- I/O ---------------------------------------------------------------------
def log(*args):
    print("[%s]" % time.strftime("%Y-%m-%dT%H:%M:%S"), *args, flush=True)


def _request(url, method="GET", headers=None, data=None):
    """One HTTP call. Always sends a User-Agent (Discord Cloudflare 1010-403s without one)."""
    hdrs = {"User-Agent": "autofix-bridge"}
    if headers:
        hdrs.update(headers)
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, headers=hdrs, data=body, method=method)
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:  # noqa: S310 (internal URLs)
        raw = resp.read()
        return json.loads(raw) if raw else None


def post_discord(msg):
    if not DISCORD_WEBHOOK_URL:
        log("WARN: no Discord webhook set; skipping report:", msg)
        return
    try:
        _request(DISCORD_WEBHOOK_URL, method="POST", data={"content": msg})
    except Exception as e:  # best-effort report; never crash the loop
        log("discord post failed (%s):" % msg, e)


def push(ok, msg):
    if not KUMA_PUSH:
        log("WARN: no push token set; skipping push:", msg)
        return
    qs = urllib.parse.urlencode({"status": "up" if ok else "down", "msg": msg})
    try:
        _request("%s/api/push/%s?%s" % (KUMA_URL, KUMA_PUSH, qs))
    except Exception as e:  # best-effort heartbeat; never crash the loop
        log("push failed (%s):" % msg, e)


def touch_heartbeat():
    try:
        with open(HEARTBEAT_FILE, "w") as fh:
            fh.write("%s\n" % time.time())
    except OSError as e:
        log("WARN: heartbeat write failed:", e)


def run_once(streaks):
    """One poll+decide+act cycle. Returns (ok, msg) for the Kuma push. Raises on an
    unreachable *arr / failed mutation, which main() converts to a descriptive `down`."""
    apps = [
        (
            "Sonarr",
            SONARR_URL,
            SONARR_URL + "/api/v3/queue?includeUnknownSeriesItems=true&pageSize=250",
            SONARR_API_KEY,
        ),
        (
            "Radarr",
            RADARR_URL,
            RADARR_URL + "/api/v3/queue?includeUnknownMovieItems=true&pageSize=250",
            RADARR_API_KEY,
        ),
    ]
    configured = [a for a in apps if a[3]]
    if not configured:
        return True, "arr auto-block disabled (no API keys)"

    candidates = {}  # item_key -> (app_name, base, key, item)
    for app_name, base, url, key in configured:
        data = _request(url, headers={"X-Api-Key": key})
        for item in data.get("records", []):
            if is_candidate(item, DANGEROUS_MSG_PATTERNS, CLIENT_ERROR_PATTERNS):
                candidates[item_key(app_name, item)] = (app_name, base, key, item)

    to_act, held = eligible(
        set(candidates), streaks, GRACE_CYCLES, MAX_ACTIONS_PER_CYCLE
    )
    if held:
        msg = "%d queue items eligible — holding (max %d/cycle), investigate" % (
            len(held),
            MAX_ACTIONS_PER_CYCLE,
        )
        post_discord(msg)
        return False, msg

    acted = 0
    for k in to_act:
        app_name, base, key, item = candidates[k]
        streak = streaks.get(k, GRACE_CYCLES)
        if not DRY_RUN:
            # Mutate FIRST: if the DELETE/search raises, it must propagate (main() renders a
            # Kuma `down`) without a false "Blocklisted" report having already gone to Discord.
            _request(
                "%s/api/v3/queue/%s?removeFromClient=true&blocklist=true"
                % (base, item["id"]),
                method="DELETE",
                headers={"X-Api-Key": key},
            )
            cmd = search_command(app_name, item)
            if cmd:
                _request(
                    base + "/api/v3/command",
                    method="POST",
                    headers={"X-Api-Key": key},
                    data=cmd,
                )
        report = format_action(
            DRY_RUN,
            app_name,
            item.get("title") or "?",
            item_reason(item),
            streak,
            GRACE_CYCLES,
        )
        log(report)
        post_discord(report)
        acted += 1

    if acted:
        verb = "would act on" if DRY_RUN else "acted on"
        return True, "%s %d queue item(s)" % (verb, acted)
    return True, "queue clean (%s)" % ", ".join(a[0] for a in configured)


def scan_series_fakes(base, key):
    """Every fake-remux episodefile across the Sonarr library. One /api/v3/episodefile call per
    series (that endpoint requires a seriesId), so this is a daily-cadence scan, not per-cycle."""
    series = _request(base + "/api/v3/series", headers={"X-Api-Key": key}) or []
    fakes = []
    for s in series:
        efs = (
            _request(
                "%s/api/v3/episodefile?seriesId=%s" % (base, s.get("id")),
                headers={"X-Api-Key": key},
            )
            or []
        )
        fakes.extend(fake_files(efs, s.get("title") or "?"))
    return fakes


def run_fake_remux_scan():
    """Scan Sonarr's library for fake remuxes and, unless FAKEREMUX_DRY_RUN, DELETE each file +
    re-search its series (the NTRX-style block in the Anime profile then keeps the re-grab from
    re-fetching the same fake). Best-effort — main() catches a raise so a scan error can't kill the
    queue loop. Returns a one-line summary."""
    if not SONARR_API_KEY:
        return "disabled (no Sonarr key)"
    fakes = scan_series_fakes(SONARR_URL, SONARR_API_KEY)
    if not fakes:
        return "library clean"
    if len(fakes) > FAKEREMUX_MAX_PER_SCAN:
        # A whole-library match is a rule bug or a systemic import setting, not N independent bad
        # grabs — never mass-delete the library; alert and let a human look.
        msg = "%d fake remuxes found — holding (max %d/scan), investigate" % (
            len(fakes),
            FAKEREMUX_MAX_PER_SCAN,
        )
        post_discord(msg)
        return msg
    verb = "WOULD delete+re-search" if FAKEREMUX_DRY_RUN else "Deleted+re-searched"
    to_search = set()
    for f in fakes:
        if not FAKEREMUX_DRY_RUN:
            # Delete FIRST so a failure propagates before its report is posted (mirrors run_once).
            _request(
                "%s/api/v3/episodefile/%s" % (SONARR_URL, f["fileId"]),
                method="DELETE",
                headers={"X-Api-Key": SONARR_API_KEY},
            )
            to_search.add(f["seriesId"])
        line = "%s [Sonarr] %s — %s but %s" % (
            verb,
            sanitize(f["path"]),
            sanitize(f["quality"]),
            sanitize(f["codec"]),
        )
        log(line)
        post_discord(line)
    for sid in sorted(to_search):
        _request(
            SONARR_URL + "/api/v3/command",
            method="POST",
            headers={"X-Api-Key": SONARR_API_KEY},
            data={"name": "SeriesSearch", "seriesId": sid},
        )
    return "%s %d fake remux(es)" % (verb.lower(), len(fakes))


def main():
    once = "--once" in sys.argv
    streaks = {}
    last_fakeremux_scan = 0.0
    log(
        "autofix-bridge starting (interval=%ss, dry_run=%s, fakeremux_dry_run=%s, once=%s)"
        % (INTERVAL, DRY_RUN, FAKEREMUX_DRY_RUN, once)
    )
    while True:
        try:
            ok, msg = run_once(streaks)
        except (
            Exception
        ) as e:  # an unreachable *arr / failed mutation must not kill the loop
            ok, msg = False, "autofix-bridge error: %s" % e
        log("OK  " if ok else "DOWN", msg)
        push(ok, msg)
        touch_heartbeat()

        now = time.time()
        if once or now - last_fakeremux_scan >= FAKEREMUX_SCAN_INTERVAL:
            last_fakeremux_scan = now
            try:
                log("fake-remux:", run_fake_remux_scan())
            except (
                Exception
            ) as e:  # best-effort — a scan error must not kill the queue loop
                log("fake-remux scan error:", e)

        if once:
            break
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
