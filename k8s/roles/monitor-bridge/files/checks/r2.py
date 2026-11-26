"""Cloudflare R2 checks for monitor-bridge: free-tier headroom, and the etcd snapshot lock.

Reads config as `cfg.X` and queries R2 through the `src` argument (`bridge.sources.Sources`),
which a test replaces with a fake. Its caches are `src.state.r2_probe` and
`src.state.r2_lock_probe` (`bridge.streaks.State`). Rule and enforcement: bridge/config.py's
header.
"""

import json
import re
import time
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone

from bridge import sigv4
from bridge.config import Config
from bridge.sources import Sources
from bridge.parsing import FETCH_BODY_MAX
from bridge.types import CheckResult, JsonValue, as_list, as_object, as_object_list
from verdicts.storage import (
    R2_CANARY_PREFIX,
    r2_canary_plan,
    r2_classify_operations,
    r2_lock_verdict,
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
    cfg: Config, src: Sources, now: float
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
        # Storage is a point-in-time series, so take the most recent bucket and look back far
        # enough; `datetime` is selected as a dimension because orderBy needs one.
        "storage_since": json.dumps(
            (datetime.fromtimestamp(now, timezone.utc) - timedelta(days=2)).strftime(
                fmt
            )
        ),
    }
    data = as_object(
        src.post_json(
            cfg.CF_GRAPHQL_URL,
            {"query": query},
            headers={"Authorization": "Bearer %s" % cfg.CF_ANALYTICS_TOKEN},
        ),
        "Cloudflare GraphQL response",
    )
    # Cloudflare answers 200 with a populated `errors` on a bad query or under-scoped token;
    # unchecked it would read as a zero-usage bucket.
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


def check_r2_usage(
    cfg: Config, src: Sources, now: float | None = None
) -> tuple[bool, str]:
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
        src.state.r2_probe["ts"] is not None
        and src.state.r2_probe["ok"]
        and now - src.state.r2_probe["ts"] < cfg.R2_PROBE_INTERVAL_S
    ):
        return True, "%s (checked %.0fm ago)" % (
            src.state.r2_probe["msg"],
            (now - src.state.r2_probe["ts"]) / 60,
        )
    storage_bytes, uploads, class_a, class_b, unknown = r2_query_usage(cfg, src, now)
    result = r2_usage_verdict(
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
    src.state.r2_probe["ts"] = now
    src.state.r2_probe["ok"], src.state.r2_probe["msg"] = result
    return result


def _r2_send(cfg: Config, src: Sources, method: str, path: str, now: float) -> bytes:
    """A SigV4-signed request to the bucket's S3 API; `path` follows the bucket name."""
    url = "https://%s.r2.cloudflarestorage.com/%s%s" % (
        cfg.CF_ACCOUNT_ID,
        cfg.R2_BUCKET,
        path,
    )
    return src.send(
        method,
        url,
        sigv4.sign(method, url, cfg.R2_S3_KEY_ID, cfg.R2_S3_SECRET_KEY, now=now),
        # A PUT carries an empty body so urllib sends Content-Length: 0.
        b"" if method == "PUT" else None,
    )


def _r2_delete_refused(cfg: Config, src: Sources, key: str, now: float) -> bool:
    """Whether R2's lock refused to delete `key`. Any answer but a delete or that 409 raises."""
    try:
        _r2_send(cfg, src, "DELETE", "/" + key, now)
    except urllib.error.HTTPError as e:
        if e.code == 409 and "ObjectLockedByBucketPolicy" in str(e.msg):
            return True
        raise
    return False


def _r2_etcd_lock_probe(cfg: Config, src: Sources, now: float) -> CheckResult:
    listing = _r2_send(
        cfg,
        src,
        "GET",
        "/?list-type=2&prefix=" + urllib.parse.quote(R2_CANARY_PREFIX, safe=""),
        now,
    ).decode("utf-8", "replace")
    plan = r2_canary_plan(
        re.findall(r"<Key>([^<]*)</Key>", listing),
        datetime.fromtimestamp(now, timezone.utc).date(),
        cfg.R2_ETCD_LOCK_DAYS,
    )
    if plan.upload:
        # Zero bytes: k3s lists every non-empty object under etcd-snapshots/ as a ready snapshot,
        # which would mask a stale one from check 12.
        _r2_send(cfg, src, "PUT", "/" + plan.upload, now)
    probe_refused = _r2_delete_refused(cfg, src, plan.probe, now)
    expired_refused = [
        key for key in plan.expired if _r2_delete_refused(cfg, src, key, now)
    ]
    return r2_lock_verdict(
        plan.probe, probe_refused, expired_refused, cfg.R2_ETCD_LOCK_DAYS
    )


def check_r2_etcd_lock(
    cfg: Config, src: Sources, now: float | None = None
) -> tuple[bool, str]:
    """Daily proof of ADR-0041's R2 lock rule over etcd-snapshots/, by its effect. (ok, msg).

    R2's S3 API cannot read a lock rule, so the check uploads a zero-byte canary under the
    prefix once a day and tries to delete an earlier one: R2's 409 `ObjectLockedByBucketPolicy`
    means the rule holds, and a delete that succeeds means it does not. Canaries past the
    lock's age are deleted, and a refusal there means a lock outlasts the rule's age.

    A verdict, or a 4xx refusal, is cached for R2_LOCK_PROBE_INTERVAL_S, so a missing lock does
    not upload and delete a canary every cycle. A transport failure, a 5xx or a 429 raises,
    which exports a down row, and the next cycle probes again. Empty credentials disable the
    check.
    """
    if not (
        cfg.CF_ACCOUNT_ID
        and cfg.R2_BUCKET
        and cfg.R2_S3_KEY_ID
        and cfg.R2_S3_SECRET_KEY
    ):
        return True, "R2 etcd lock check disabled (no account id / bucket / S3 key)"
    now = now if now is not None else time.time()
    cache = src.state.r2_lock_probe
    if cache["ts"] is not None and now - cache["ts"] < cfg.R2_LOCK_PROBE_INTERVAL_S:
        return cache["ok"], "%s (checked %.0fh ago)" % (
            cache["msg"],
            (now - cache["ts"]) / 3600,
        )
    try:
        result = _r2_etcd_lock_probe(cfg, src, now)
    except urllib.error.HTTPError as e:
        if e.code >= 500 or e.code == 429:
            raise
        result = CheckResult(
            False, "R2 refused the lock canary: %s" % e, ["request refused"]
        )
    cache["ts"] = now
    cache["ok"], cache["msg"] = result
    return result
