"""Service-health verdicts for check.py — n8n, the *arrs, Prowlarr, HA, Loki, GitOps, Discord.

These decide; check.py fetches. Each takes its inputs as arguments and reads no module-level
config, which is what makes it safe to live here — see bridge_parsing.py's header for the rule
and why breaking it fails silently rather than loudly.

`n8n_update_streaks` is the one carrying state across cycles rather than judging a snapshot:
n8n does not save successful executions, so a consecutive-failure streak cannot be read from
one poll. It takes the state dict as an argument, which is what keeps it testable and lets it
live here.
"""

from datetime import datetime, timedelta, timezone

from bridge_common import sanitize
from bridge_parsing import parse_rfc3339


def n8n_update_streaks(workflows_json, executions_json, state, now, window_s):
    """Advance per-workflow consecutive-failure streaks across check cycles.

    n8n doesn't record successful executions (EXECUTIONS_DATA_SAVE_ON_SUCCESS=none), so a
    streak can't be read from one snapshot — it's accumulated here. Per active ("Prod")
    workflow we find its most-recent error execution; the streak advances by one each time
    that id is NEW (a fresh failure since last cycle, so a single lingering failure isn't
    double-counted across cycles), and resets to 0 once the most-recent error ages past
    `window_s` (recovered / went idle) or no error is on record. `state` is a mutable
    {workflow_id: {"last_id", "streak"}} dict persisted across cycles. Returns
    {workflow_name: streak} for streak >= 1. Pure given (state, now) — unit-tested by
    driving cycles.
    """
    active = {
        w["id"]: (w.get("name") or w["id"])
        for w in workflows_json.get("data", [])
        if w.get("active")
    }
    latest = {}
    for ex in executions_json.get("data", []):
        wid = ex.get("workflowId")
        if wid not in active:
            continue
        ts = ex.get("stoppedAt") or ex.get("startedAt")
        if not ts:
            continue
        cur = latest.get(wid)
        if cur is None or ts > cur[1]:  # RFC3339 'Z' timestamps sort lexicographically
            latest[wid] = (ex.get("id"), ts)
    for wid in list(state):  # forget workflows that are no longer active
        if wid not in active:
            del state[wid]
    cutoff = now - timedelta(seconds=window_s)
    result = {}
    for wid, name in active.items():
        st = state.setdefault(wid, {"last_id": None, "streak": 0})
        info = latest.get(wid)
        if info is None:
            st["last_id"], st["streak"] = None, 0
            continue
        eid, ts = info
        dt = parse_rfc3339(ts)
        if (
            dt.tzinfo is None
        ):  # n8n emits UTC 'Z'; assume UTC if a naive ts slips through
            dt = dt.replace(tzinfo=timezone.utc)
        if dt < cutoff:
            st["last_id"], st["streak"] = None, 0
            continue
        if eid != st["last_id"]:
            st["streak"] += 1
            st["last_id"] = eid
        result[name] = st["streak"]
    return result


def n8n_verdict(streaks, consecutive_max, systemic_streak, systemic_max):
    """Pure: turn per-workflow failure streaks into an up/down verdict + message.

    Down if any single workflow has failed >= consecutive_max times in a row, OR if
    >= systemic_max workflows are each failing >= systemic_streak times — the n8n-wide catch
    that pages promptly as ONE alert (a broken n8n) instead of waiting for each workflow to
    reach consecutive_max, and instead of a per-workflow flood.
    """
    if not streaks:
        return True, "no active-workflow failures"
    ranked = sorted(streaks.items(), key=lambda nc: (-nc[1], nc[0]))
    systemic = [(n, c) for n, c in ranked if c >= systemic_streak]
    if len(systemic) >= systemic_max:
        names = ", ".join("%s (%d)" % (sanitize(n), c) for n, c in systemic[:5])
        return False, "n8n systemic: %d workflows failing repeatedly (%s)" % (
            len(systemic),
            names,
        )
    broken = [(n, c) for n, c in ranked if c >= consecutive_max]
    if broken:
        desc = ", ".join("%s (%d)" % (sanitize(n), c) for n, c in broken)
        return False, "n8n: %d active workflow(s) failed %d+ consecutive: %s" % (
            len(broken),
            consecutive_max,
            desc,
        )
    return True, "%d active workflow(s) failing (< %d consecutive)" % (
        len(ranked),
        consecutive_max,
    )


def gitops_alive(age_s, max_age_s):
    """Pure: is the deployer's last completed tick recent enough? Returns (ok, msg)."""
    if age_s <= max_age_s:
        return True, "deployer ran %.0fm ago" % (age_s / 60)
    return False, "deployer last ran %.0fm ago (> %.0fm)" % (age_s / 60, max_age_s / 60)


def queue_warnings(queue_json, app_name):
    """Pure: (app_name, title, reason) for each queue item needing an operator's eyes.

    Fed a sonarr/radarr /api/v3/queue payload. trackedDownloadStatus == "warning" is the
    2026-07-01 incident's signal — the *arr blocked the import itself but only flagged the
    queue item, so it kept seeding for a day with nothing paging. "error" is the harder
    sibling status (upstream enum: ok/warning/error) — at least as actionable, previously
    skipped. trackedDownloadState == "importBlocked" is the harder-blocked sibling state,
    "importFailed" its attempted-and-failed counterpart (both from the upstream
    TrackedDownloadState enum); "importPending" WITH statusMessages covers the case where
    the block reason shows up under the pending state instead. Plain "importPending" with
    no messages is the ordinary just-finished-download queue waiting its turn — not a
    problem, so it's left alone.
    """
    offenders = []
    for item in queue_json.get("records", []):
        status = item.get("trackedDownloadStatus")
        state = item.get("trackedDownloadState")
        messages = item.get("statusMessages") or []
        flagged = (
            status in ("warning", "error")
            or state in ("importBlocked", "importFailed")
            or (state == "importPending" and messages)
        )
        if not flagged:
            continue
        title = item.get("title") or "?"
        reasons = [m for sm in messages for m in sm.get("messages", [])]
        reason = "; ".join(reasons) or status or state or "warning"
        offenders.append((app_name, title, reason))
    return offenders


def indexers_down(status_json, name_by_id, now, min_down_min, ignore=None):
    """Pure: (name, minutes_down) for each Prowlarr indexer failing >= min_down_min minutes.

    Fed /api/v1/indexerstatus (a list of {indexerId, initialFailure, disabledTill, ...}) and an
    indexerId->name map from /api/v1/indexer. An indexer is listed in indexerstatus only while
    Prowlarr has it disabled due to failures; initialFailure is when the CURRENT failure run
    started, so (now - initialFailure) is the outage duration — a flap that recovers before the
    threshold drops out of the list and never qualifies. A null/absent/unparseable initialFailure
    is skipped (treated as just-started) rather than crashing the whole check. `ignore` is an
    iterable of indexer names (matched case-insensitively) that are never flagged — for
    chronically-flaky public trackers (see PROWLARR_INDEXER_IGNORE). Sorted worst-first so the
    longest outage leads the alert msg.
    """
    cutoff_s = min_down_min * 60
    ignored = {n.strip().lower() for n in (ignore or ()) if n.strip()}
    offenders = []
    for s in status_json or []:
        init = s.get("initialFailure")
        if not init:
            continue
        try:
            age_s = (now - parse_rfc3339(init)).total_seconds()
        except ValueError, TypeError:
            continue
        if age_s >= cutoff_s:
            iid = s.get("indexerId")
            name = name_by_id.get(iid) or "indexer %s" % iid
            if name.strip().lower() in ignored:
                continue
            offenders.append((name, age_s / 60.0))
    offenders.sort(key=lambda nm: -nm[1])
    return offenders


def ha_heartbeat_fresh(state, max_age_s, now=None):
    """`state` is HA's /api/states/input_datetime.ha_heartbeat payload.

    Its last_changed advances every minute only while HA's automation scheduler runs the
    heartbeat automation, so a stale (or missing) last_changed means HA is wedged or the
    automation never resumed after a restart — invisible to the HTTP healthcheck.
    """
    now = now or datetime.now(timezone.utc)
    lc = (state or {}).get("last_changed")
    if not lc:
        return False, "no heartbeat state (entity missing or never set)"
    age = (now - parse_rfc3339(lc)).total_seconds()
    if age > max_age_s:
        return False, "stale — automations last ran %.0fs ago (> %gs)" % (
            age,
            max_age_s,
        )
    return True, "fresh — automations ran %.0fs ago" % age


def ha_ban_verdict(banned_count, window):
    """Decide the ip_ban arm from the "Banned IP" line count over `window` (None = no series).

    None and 0 are the SAME healthy answer here, unlike loki_ingestion_fresh where a silent
    stream is itself the fault: HA logs nothing when it bans nobody, so an empty vector is what
    a healthy cluster looks like.
    """
    if not banned_count:
        return True, "no ip_ban events in %s" % window
    return False, (
        "HA ip_ban fired %d time(s) in %s — an internal source IP is likely banned and is now "
        "getting 403s; check the pod log for the address and delete its line from "
        "/config/ip_bans.yaml to clear" % (int(banned_count), window)
    )


def loki_ingestion_fresh(count, window):
    """Decide log-pipeline freshness from the line count over `window` (None = no series)."""
    if not count:  # None or 0 — nothing shipped: promtail dead, positions corrupt, etc.
        return (
            False,
            "no log lines ingested in %s — promtail/Loki pipeline silent" % window,
        )
    return True, "%d log lines in %s" % (int(count), window)


def promtail_dropped(count, window, threshold):
    """Pure: did promtail drop more than `threshold` entries over `window`? (ok, msg).

    `count` = sum(increase(promtail_dropped_entries_total[window])) over ALL drop reasons
    (ingester_error / rate_limited / stream_limited / line_too_long), None when the counter has no
    series (reads as 0). Above the threshold means Loki was rejecting entries and promtail gave up on
    them — partial log loss the total-silence Loki Log Ingestion check can't see.
    """
    n = count or 0.0
    if n > threshold:
        return False, (
            "promtail dropped %.0f log entries in %s (> %.0f) — partial log loss"
            % (n, window, threshold)
        )
    return True, "promtail drops ok (%.0f in %s)" % (n, window)


def discord_webhook_ok(status_code, name=None):
    """Pure: does a GET on a Discord webhook return 200 (still valid)? (ok, msg).

    Discord answers a webhook GET with its JSON metadata (id/name) and HTTP 200 while the
    webhook exists, and 404 once it's been rotated/revoked/deleted — so a non-200 means the
    alert POSTs won't deliver. (A GET never posts a message, so this can't spam.)
    """
    if status_code == 200:
        return True, "Discord webhook valid%s" % (" (%s)" % name if name else "")
    return (
        False,
        "Discord webhook returned HTTP %s — alerts won't deliver" % status_code,
    )
