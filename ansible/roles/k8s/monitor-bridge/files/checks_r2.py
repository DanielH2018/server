"""Cloudflare R2 free-tier headroom for monitor-bridge — one GraphQL query, four arms.

Reads config as `cfg.X` and the fetch layer as `bridge_io.X`, so the tests' patches on those
modules reach it. `_r2_probe` lives beside `r2_usage`, the only code that mutates it. Rule and
enforcement: bridge_config.py's header.
"""

import json
import time
from datetime import datetime, timedelta, timezone

import bridge_config as cfg
import bridge_io
from bridge_parsing import FETCH_BODY_MAX


def r2_month_start(now):
    """UTC midnight on the 1st of the calendar month containing `now` (epoch seconds).

    R2's free tier resets on the calendar month, so month-to-date is the only window whose
    percentages mean anything — a rolling 30d window would report headroom that does not exist
    on the 2nd and headroom that has already been given back on the 30th.
    """
    d = datetime.fromtimestamp(now, timezone.utc)
    return d.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def r2_classify_operations(rows):
    """(class_a, class_b, unknown_actions) from r2OperationsAdaptiveGroups rows.

    An actionType in neither published class list counts toward CLASS A — the expensive one — and
    is named in the verdict. Cloudflare adds operations over time, and the alternative readings are
    both worse: counting an unknown as Class B under-reports the arm with a 10x tighter limit, and
    dropping it makes new operations invisible. Over-counting reports headroom we do not have,
    which is the direction a guard should err in, and the named action says why the numbers moved.
    """
    class_a = class_b = 0
    unknown = {}
    for row in rows:
        action = (row.get("dimensions") or {}).get("actionType") or "unknown"
        requests = (row.get("sum") or {}).get("requests") or 0
        if action in cfg.R2_CLASS_B_ACTIONS:
            class_b += requests
        elif action in cfg.R2_FREE_ACTIONS:
            continue
        elif action in cfg.R2_CLASS_A_ACTIONS:
            class_a += requests
        else:
            class_a += requests
            unknown[action] = unknown.get(action, 0) + requests
    return class_a, class_b, sorted(unknown)


def _pct(used, limit):
    """Percent of `limit` used, or None when the limit is disabled (<= 0)."""
    if limit <= 0:
        return None
    return 100.0 * used / limit


def r2_usage_verdict(
    storage_bytes,
    uploads,
    class_a,
    class_b,
    unknown_actions,
    storage_max_gb=None,
    class_a_max=None,
    class_b_max=None,
    uploads_max=None,
    max_pct=None,
):
    """(ok, msg) from month-to-date R2 usage against the free-tier limits.

    Reports all three arms every cycle whether or not any breaches, so the Kuma message carries
    the trend and not just the alarm — the point of the monitor is to see a runaway client early.
    """
    storage_max_gb = cfg.R2_STORAGE_MAX_GB if storage_max_gb is None else storage_max_gb
    class_a_max = cfg.R2_CLASS_A_MAX if class_a_max is None else class_a_max
    class_b_max = cfg.R2_CLASS_B_MAX if class_b_max is None else class_b_max
    uploads_max = cfg.R2_UPLOADS_MAX if uploads_max is None else uploads_max
    max_pct = cfg.R2_USAGE_MAX_PCT if max_pct is None else max_pct

    storage_gb = storage_bytes / 1e9  # R2 bills decimal GB, not GiB
    arms = (
        ("storage", storage_gb, storage_max_gb, "%.2f/%.0f GB"),
        ("Class A", class_a, class_a_max, "%.0f/%.0f"),
        ("Class B", class_b, class_b_max, "%.0f/%.0f"),
    )
    parts = []
    breaching = []
    for label, used, limit, fmt in arms:
        pct = _pct(used, limit)
        if pct is None:
            parts.append("%s %s (no limit set)" % (label, fmt % (used, limit)))
            continue
        parts.append("%s %s (%.0f%%)" % (label, fmt % (used, limit), pct))
        if pct >= max_pct:
            breaching.append("%s at %.0f%%" % (label, pct))

    if uploads_max > 0 and uploads > uploads_max:
        breaching.append(
            "%d incomplete multipart uploads (they bill as storage and do not show in a "
            "listing — check the bucket's AbortIncompleteMultipartUpload lifecycle rule)"
            % uploads
        )

    msg = "R2 month-to-date: " + ", ".join(parts)
    if unknown_actions:
        msg += " [unclassified ops counted as Class A: %s]" % ", ".join(unknown_actions)
    if breaching:
        return False, "over %.0f%% of free tier — %s. %s" % (
            max_pct,
            "; ".join(breaching),
            msg,
        )
    return True, msg


R2_QUERY = """query {
  viewer {
    accounts(filter: {accountTag: %(account)s}) {
      storage: r2StorageAdaptiveGroups(
        limit: 1
        filter: {bucketName: %(bucket)s, datetime_geq: %(storage_since)s}
        orderBy: [datetime_DESC]
      ) {
        max { payloadSize metadataSize uploadCount }
        dimensions { datetime }
      }
      operations: r2OperationsAdaptiveGroups(
        limit: 100
        filter: {bucketName: %(bucket)s, datetime_geq: %(month_start)s}
      ) {
        dimensions { actionType }
        sum { requests }
      }
    }
  }
}"""


def r2_query_usage(now):
    """(storage_bytes, uploads, class_a, class_b, unknown_actions) for the current month.

    One POST for both datasets — same account scope, so splitting it would double the calls and
    the error paths for nothing.
    """
    month_start = r2_month_start(now)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    query = R2_QUERY % {
        "account": json.dumps(cfg.CF_ACCOUNT_ID),
        "bucket": json.dumps(cfg.R2_BUCKET),
        "month_start": json.dumps(month_start.strftime(fmt)),
        # Storage is a point-in-time series, not a sum: one row per datetime bucket, so take the
        # most recent and look back far enough that a quiet bucket still has one. `datetime` is
        # selected as a dimension because the orderBy key has to be one — Cloudflare's own storage
        # example does exactly this, and dropping it risks a rejected query, which this check
        # would then report as `down` every cycle.
        "storage_since": json.dumps(
            (datetime.fromtimestamp(now, timezone.utc) - timedelta(days=2)).strftime(
                fmt
            )
        ),
    }
    data = bridge_io._post_json(
        cfg.CF_GRAPHQL_URL,
        {"query": query},
        headers={"Authorization": "Bearer %s" % cfg.CF_ANALYTICS_TOKEN},
    )
    # Cloudflare answers 200 with a populated `errors` on a bad query or an under-scoped token, so
    # this is the only place a wrong token surfaces. Left unchecked it would read as a zero-usage
    # bucket — a monitor green because it is blind, the failure this file keeps re-learning.
    errors = data.get("errors")
    if errors:
        raise RuntimeError(
            "Cloudflare GraphQL: %s"
            % "; ".join(str(e.get("message", e)) for e in errors)[:FETCH_BODY_MAX]
        )
    accounts = ((data.get("data") or {}).get("viewer") or {}).get("accounts") or []
    if not accounts:
        raise RuntimeError(
            "Cloudflare GraphQL returned no account for accountTag — wrong CF_ACCOUNT_ID, "
            "or the token is not scoped to this account"
        )
    account = accounts[0]
    storage_rows = account.get("storage") or []
    if storage_rows:
        peak = storage_rows[0].get("max") or {}
        storage_bytes = (peak.get("payloadSize") or 0) + (peak.get("metadataSize") or 0)
        uploads = peak.get("uploadCount") or 0
    else:
        # An empty bucket genuinely reports no storage rows; that is 0 bytes, not a fault.
        storage_bytes = uploads = 0
    class_a, class_b, unknown = r2_classify_operations(account.get("operations") or [])
    return storage_bytes, uploads, class_a, class_b, unknown


# ts=None means never probed. An explicit sentinel rather than 0.0: "0 seconds since the epoch" is
# indistinguishable from a real timestamp by the arithmetic below, and only the sheer size of a
# real time.time() keeps that from reading as a fresh cache entry on the first cycle.
_r2_probe = {"ts": None, "ok": True, "msg": ""}


def r2_usage(now=None):
    """Throttled R2 free-tier headroom check. (ok, msg).

    SUCCESSES are cached for R2_PROBE_INTERVAL_S — month-to-date aggregates do not move on a 300s
    cycle, and Cloudflare's GraphQL API is rate-limited per account. A FAILURE is not cached: these
    calls are free and count against no R2 budget, so unlike b2_reachable there is nothing to
    protect by holding a stale verdict, and a re-probe next cycle detects recovery sooner. The
    one-cycle blip that re-probing would otherwise page on is absorbed by STARTUP_GRACE.
    """
    if not cfg.CF_ACCOUNT_ID or not cfg.CF_ANALYTICS_TOKEN or not cfg.R2_BUCKET:
        return True, "R2 usage check disabled (no account id / token / bucket)"
    now = now if now is not None else time.time()
    if (
        _r2_probe["ts"] is not None
        and _r2_probe["ok"]
        and now - _r2_probe["ts"] < cfg.R2_PROBE_INTERVAL_S
    ):
        return True, "%s (checked %.0fm ago)" % (
            _r2_probe["msg"],
            (now - _r2_probe["ts"]) / 60,
        )
    storage_bytes, uploads, class_a, class_b, unknown = r2_query_usage(now)
    ok, msg = r2_usage_verdict(storage_bytes, uploads, class_a, class_b, unknown)
    _r2_probe["ts"] = now
    _r2_probe["ok"] = ok
    _r2_probe["msg"] = msg
    return ok, msg


def check_r2_usage():
    return r2_usage()
