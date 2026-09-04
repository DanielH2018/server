"""Cloudflare R2 free-tier headroom for monitor-bridge — one GraphQL query, four arms.

Reads config as `cfg.X` and the fetch layer as `bridge.net.X`, so the tests' patches on those
modules reach it. `_r2_probe` lives beside `r2_usage`, the only code that mutates it. Rule and
enforcement: bridge/config.py's header.
"""

import json
import time
from datetime import datetime, timedelta, timezone

import bridge.config as cfg
import bridge.net
from bridge.parsing import FETCH_BODY_MAX
from verdicts.storage import (
    r2_classify_operations,
    r2_month_start,
    r2_usage_verdict,
)


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


def r2_query_usage(now: float) -> tuple[float, float, float, float, list[str]]:
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
    data = bridge.net._post_json(
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


def r2_usage(now: float | None = None) -> tuple[bool, str]:
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
    ok, msg = r2_usage_verdict(
        storage_bytes,
        uploads,
        class_a,
        class_b,
        unknown,
        cfg.R2_STORAGE_MAX_GB,
        cfg.R2_CLASS_A_MAX,
        cfg.R2_CLASS_B_MAX,
        cfg.R2_UPLOADS_MAX,
        cfg.R2_USAGE_MAX_PCT,
    )
    _r2_probe["ts"] = now
    _r2_probe["ok"] = ok
    _r2_probe["msg"] = msg
    return ok, msg


def check_r2_usage() -> tuple[bool, str]:
    return r2_usage()
