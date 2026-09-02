"""Backblaze B2 checks for monitor-bridge — reachability (the B2_DEPENDENT gate) and storage usage.

Reads config as `cfg.X` and the fetch layer as `bridge_io.X`, so the tests' patches on those
modules reach it; `b2_reachable` and `b2_authorize` are patched on THIS module, where
`check_b2_reachable` reads them. The probe caches `_b2_probe` / `_b2_storage` live beside the
code that mutates them. Rule and enforcement: bridge_config.py's header.
"""

import base64
import time
import urllib.error
import urllib.parse

import bridge_config as cfg
import bridge_io


# `ttl` is how long THIS cached verdict is held, chosen per outcome by b2_reachable — a billed
# answer from B2 holds B2_PROBE_INTERVAL_S, a transport failure holds B2_TRANSPORT_RETRY_S.
_b2_probe = {
    "ts": 0.0,
    "ok": True,
    "msg": "not yet probed",
    "ttl": cfg.B2_PROBE_INTERVAL_S,
}
_b2_storage = {"ts": 0.0, "ok": False, "msg": "not yet probed"}


def b2_authorize_data():
    """The parsed b2_authorize_account response. Raises on any transport/HTTP failure."""
    token = base64.b64encode(
        ("%s:%s" % (cfg.B2_PROBE_KEY_ID, cfg.B2_PROBE_APPLICATION_KEY)).encode()
    ).decode()
    return bridge_io._get_json(
        cfg.B2_PROBE_URL, headers={"Authorization": "Basic %s" % token}
    )


def b2_storage_api(auth):
    """(api_url, authorization_token, bucket_id) from an authorize response.

    v3 groups the storage endpoint under `apiInfo.storageApi` where v1/v2 had `apiUrl` at the top
    level, so both shapes are read — the same version-tolerance b2_authorize applies to its own
    fields. `bucketId` is present when the application key is bucket-scoped, which this one is;
    without it there is no bucket to sum and the caller reports that rather than guessing.
    """
    storage = (auth.get("apiInfo") or {}).get("storageApi") or {}
    api_url = storage.get("apiUrl") or auth.get("apiUrl")
    bucket_id = storage.get("bucketId") or (auth.get("allowed") or {}).get("bucketId")
    return api_url, auth.get("authorizationToken"), bucket_id


def b2_sum_versions(pages):
    """(total_bytes, version_count) over an iterable of b2_list_file_versions payloads.

    Sums `contentLength` across ALL versions, including hidden ones and the unfinished large-file
    parts that a plain object listing omits — those bill as stored bytes, and omitting them is the
    specific way this number reads lower than the invoice.
    """
    total = 0
    count = 0
    for page in pages:
        for f in page.get("files") or []:
            size = f.get("contentLength")
            if size is None:
                size = f.get("size")
            total += int(size or 0)
            count += 1
    return total, count


def b2_storage_verdict(used_bytes, versions, truncated, cap=None, max_pct=None):
    """(ok, msg) for B2 storage headroom against the free-tier cap."""
    cap = cfg.B2_STORAGE_CAP_BYTES if cap is None else cap
    max_pct = cfg.B2_STORAGE_MAX_PCT if max_pct is None else max_pct
    if not cap:
        return False, "B2 storage cap not configured"
    pct = 100.0 * used_bytes / cap
    detail = "%.2f GB of %.0f GB (%.0f%%), %d versions" % (
        used_bytes / 1000**3,
        cap / 1000**3,
        pct,
        versions,
    )
    if truncated:
        # Under-reporting is the dangerous direction, so a truncated walk is a failure, not a
        # smaller number reported confidently.
        return (
            False,
            "B2 storage listing truncated at %d pages — %s is a FLOOR, not the total"
            % (
                cfg.B2_STORAGE_MAX_PAGES,
                detail,
            ),
        )
    if pct > max_pct:
        return False, "B2 storage over %.0f%%: %s" % (max_pct, detail)
    return True, "B2 storage %s" % detail


def b2_storage_usage(now=None):
    """Throttled B2 storage-headroom probe. (ok, msg).

    SUCCESSES are cached for B2_STORAGE_INTERVAL_S and a failure is not, the
    EMAIL_PROBE_INTERVAL_S idiom rather than b2_reachable's cache-both: a listing failure is far
    more likely to be a transient 5xx than a cap, and b2_reachable already owns the cap signal, so
    there is no spend spiral to protect against here. Empty credentials -> disabled (stays up).
    """
    if not cfg.B2_PROBE_KEY_ID or not cfg.B2_PROBE_APPLICATION_KEY:
        return True, "B2 storage check disabled (no credentials)"
    now = now if now is not None else time.time()
    if _b2_storage["ok"] and now - _b2_storage["ts"] < cfg.B2_STORAGE_INTERVAL_S:
        return _b2_storage["ok"], "%s (checked %.0fh ago)" % (
            _b2_storage["msg"],
            (now - _b2_storage["ts"]) / 3600,
        )
    try:
        api_url, token, bucket_id = b2_storage_api(b2_authorize_data())
        if not api_url or not token:
            raise RuntimeError("B2 auth response carried no storage apiUrl/token")
        if not bucket_id:
            raise RuntimeError(
                "B2 key is not bucket-scoped (no bucketId) — cannot size a bucket"
            )
        pages, truncated = b2_list_versions(api_url, token, bucket_id)
        used, versions = b2_sum_versions(pages)
        ok, msg = b2_storage_verdict(used, versions, truncated)
    except Exception as e:
        ok, msg = False, "B2 storage probe failed: %s" % e
    _b2_storage["ts"] = now
    _b2_storage["ok"] = ok
    _b2_storage["msg"] = msg
    return ok, msg


def b2_list_versions(api_url, token, bucket_id):
    """(pages, truncated) — every b2_list_file_versions page for the bucket.

    Paginates on the (nextFileName, nextFileId) cursor B2 returns; a page with neither is the
    last. Stops at B2_STORAGE_MAX_PAGES and says so, rather than looping on a cursor that never
    clears.
    """
    pages = []
    start_name = start_id = None
    for _ in range(cfg.B2_STORAGE_MAX_PAGES):
        payload = {"bucketId": bucket_id, "maxFileCount": 1000}
        if start_name:
            payload["startFileName"] = start_name
        if start_id:
            payload["startFileId"] = start_id
        page = bridge_io._post_json(
            "%s/b2api/v3/b2_list_file_versions" % api_url.rstrip("/"),
            payload,
            headers={"Authorization": token},
        )
        pages.append(page)
        start_name = page.get("nextFileName")
        start_id = page.get("nextFileId")
        if not start_name and not start_id:
            return pages, False
    return pages, True


def check_b2_storage():
    return b2_storage_usage()


def b2_authorize():
    """Authenticate against B2. (ok, msg) — the msg carries B2's own error text on failure.

    Basic auth with the key id + application key is the whole protocol for b2_authorize_account.
    _get_json re-raises HTTPError with the response body appended, so a cap breach arrives here as
    "HTTP Error 403: ... transaction_cap_exceeded ..." and that string is what reaches Kuma and
    Discord — the named cause G3 asked for.
    """
    token = base64.b64encode(
        ("%s:%s" % (cfg.B2_PROBE_KEY_ID, cfg.B2_PROBE_APPLICATION_KEY)).encode()
    ).decode()
    data = bridge_io._get_json(
        cfg.B2_PROBE_URL, headers={"Authorization": "Basic %s" % token}
    )
    # A 200 from something that isn't B2 must not read as healthy. Accept EITHER field rather than
    # pinning the response shape: Backblaze publishes a body example for v4 (accountId top-level)
    # but not for v3, whose documented change was to group endpoint info under `apiInfo`. Both
    # fields have been present since v1, so this survives a version bump either way — and a wrong
    # guess here would page every cycle rather than fail safe.
    if not (data.get("accountId") or data.get("authorizationToken")):
        return False, "B2 auth returned neither accountId nor authorizationToken"
    return True, "B2 reachable"


def b2_reachable(now=None):
    """Throttled B2 reachability probe — the gate for the B2_DEPENDENT checks. (ok, msg).

    Empty credentials -> disabled (stays up), like check_n8n's empty API key. Outcomes are cached
    rather than re-probed every cycle: unlike email_backstop, the failure being detected is a
    transaction cap, and retrying would spend more of the budget this check exists to watch. The
    cached verdict is returned (and pushed) every cycle regardless, so the push monitor's heartbeat
    stays alive and the dead-bridge watchdog isn't tripped.

    The cache TTL depends on WHERE the probe failed, because only one of the two shapes costs a B2
    transaction:

      * A response from B2 — success, or an HTTPError such as the 403 carrying
        `transaction_cap_exceeded` — reached the API and was billed. Cached for
        B2_PROBE_INTERVAL_S (30 min), so a cap breach is not re-spent every cycle.
      * Anything else (DNS, connect, timeout) never reached B2 and was billed nothing.
        _get_json wraps exactly this class as RuntimeError while re-raising HTTPError untouched,
        which is what makes the two separable here. Cached for B2_TRANSPORT_RETRY_S (one cycle).

    Without that split, one transient failure pinned the gate DOWN for the full 30 minutes: on the
    2026-08-30 restart the bridge's first cycle probed B2 before cluster egress was serving, and
    `B2 Reachable` then read DOWN for 25 minutes against an 8m35s outage — the cache was holding
    back the RECOVERY, not just the retry. Re-probing a connection that never landed is free, so
    there is nothing to protect there.

    Module-global cache, reset on container restart, like the streak counters.
    """
    if not cfg.B2_PROBE_KEY_ID or not cfg.B2_PROBE_APPLICATION_KEY:
        return True, "B2 reachability check disabled (no credentials)"
    now = now if now is not None else time.time()
    if now - _b2_probe["ts"] < _b2_probe["ttl"]:
        return _b2_probe["ok"], "%s (checked %.0fm ago)" % (
            _b2_probe["msg"],
            (now - _b2_probe["ts"]) / 60,
        )
    try:
        ok, msg = b2_authorize()
        ttl = cfg.B2_PROBE_INTERVAL_S
    except urllib.error.HTTPError as e:
        # B2 answered, so the call was billed — hold the full interval.
        ok, msg, ttl = False, "B2 unreachable: %s" % e, cfg.B2_PROBE_INTERVAL_S
    except Exception as e:
        # Never reached B2, so nothing was billed — retry on the next cycle.
        ok, msg, ttl = False, "B2 unreachable: %s" % e, cfg.B2_TRANSPORT_RETRY_S
    _b2_probe["ts"] = now
    _b2_probe["ok"] = ok
    _b2_probe["msg"] = msg
    _b2_probe["ttl"] = ttl
    return ok, msg


def check_b2_reachable():
    return b2_reachable()
