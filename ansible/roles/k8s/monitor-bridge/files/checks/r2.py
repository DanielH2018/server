"""Cloudflare R2 free-tier headroom for monitor-bridge — one GraphQL query, four arms.

Reads config as `cfg.X` and the fetch layer as `bridge.net.X`, so the tests' patches on those
modules reach it. `_r2_probe` lives beside `r2_usage`, the only code that mutates it. Rule and
enforcement: bridge/config.py's header.
"""

import json
import time
from typing import TypedDict
from datetime import datetime, timedelta, timezone

from bridge.config import Config
import bridge.net
from bridge.parsing import FETCH_BODY_MAX
from bridge.types import JsonValue, as_list, as_object, as_object_list
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


def _count(value: JsonValue) -> float:
    """A GraphQL numeric field; null reads as 0, any other non-number raises."""
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RuntimeError(
            "Cloudflare field is %s, not a number" % type(value).__name__
        )
    return value


def r2_query_usage(
    cfg: Config, now: float
) -> tuple[float, float, float, float, list[str]]:
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
    data = as_object(
        bridge.net._post_json(
            cfg.CF_GRAPHQL_URL,
            {"query": query},
            headers={"Authorization": "Bearer %s" % cfg.CF_ANALYTICS_TOKEN},
        ),
        "Cloudflare GraphQL response",
    )
    # Cloudflare answers 200 with a populated `errors` on a bad query or an under-scoped token, so
    # this is the only place a wrong token surfaces. Left unchecked it would read as a zero-usage
    # bucket — a monitor green because it is blind, the failure this file keeps re-learning.
    errors = data.get("errors")
    if errors:
        raise RuntimeError(
            "Cloudflare GraphQL: %s"
            % "; ".join(
                str(e.get("message", e)) if isinstance(e, dict) else str(e)
                for e in as_list(errors, "Cloudflare GraphQL errors")
            )[:FETCH_BODY_MAX]
        )
    viewer = as_object(
        as_object(data.get("data") or {}, "Cloudflare GraphQL data").get("viewer")
        or {},
        "Cloudflare GraphQL viewer",
    )
    accounts = as_object_list(viewer.get("accounts") or [], "Cloudflare accounts")
    if not accounts:
        raise RuntimeError(
            "Cloudflare GraphQL returned no account for accountTag — wrong CF_ACCOUNT_ID, "
            "or the token is not scoped to this account"
        )
    account = accounts[0]
    storage_rows = as_object_list(account.get("storage") or [], "Cloudflare storage")
    if storage_rows:
        peak = as_object(storage_rows[0].get("max") or {}, "Cloudflare storage max")
        storage_bytes = _count(peak.get("payloadSize")) + _count(
            peak.get("metadataSize")
        )
        uploads = _count(peak.get("uploadCount"))
    else:
        # An empty bucket genuinely reports no storage rows; that is 0 bytes, not a fault.
        storage_bytes = uploads = 0
    class_a, class_b, unknown = r2_classify_operations(
        as_object_list(account.get("operations") or [], "Cloudflare operations")
    )
    return storage_bytes, uploads, class_a, class_b, unknown


# ts=None means never probed. An explicit sentinel rather than 0.0: "0 seconds since the epoch" is
# indistinguishable from a real timestamp by the arithmetic below, and only the sheer size of a
# real time.time() keeps that from reading as a fresh cache entry on the first cycle.
class _ProbeCache(TypedDict):
    ts: float | None
    ok: bool
    msg: str


_r2_probe: _ProbeCache = {"ts": None, "ok": True, "msg": ""}


def r2_usage(cfg: Config, now: float | None = None) -> tuple[bool, str]:
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
    storage_bytes, uploads, class_a, class_b, unknown = r2_query_usage(cfg, now)
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


def check_r2_usage(cfg: Config) -> tuple[bool, str]:
    return r2_usage(cfg)
