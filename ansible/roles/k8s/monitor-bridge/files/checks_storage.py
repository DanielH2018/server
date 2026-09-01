"""Storage checks for monitor-bridge — B2 reachability and usage, R2 headroom, Longhorn
volume redundancy, PVC fullness.

Slice 4 of the check.py split. Reads config as `cfg.X` and the fetch layer as `bridge_io.X`,
so the tests' patches on those modules reach it; `b2_reachable` and `b2_authorize` are
patched on THIS module, where `check_b2_reachable` reads them. The probe caches
(`_b2_probe`, `_b2_storage`, `_r2_probe`) live beside the code that mutates them. Rule and
enforcement: bridge_config.py's header.
"""

import base64
import json
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

import bridge_config as cfg
import bridge_io
import bridge_streaks
from bridge_parsing import FETCH_BODY_MAX


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


def check_longhorn_volumes():
    """Longhorn volumes that have lost replica redundancy, named by PVC.

    `k3s_longhorn_replica_count` is 2, so a volume reading `degraded` is down to a single copy —
    still serving, one more failure from data loss — and `faulted` means no healthy replica is
    left at all. Nothing else in CHECKS covers this: k8s_workloads watches the pod, not the
    volume under it, and the backup-plane cron watches Backup objects rather than live replica
    state. Until this arm landed (2026-08-17) replica loss was silent.

    `longhorn_volume_robustness` is ONE-HOT over a `state` label (healthy/degraded/faulted/
    unknown) with value 0 or 1 — four series per volume. So this selects on the label and never
    compares the value to a state ordinal, which is the mistake an earlier proposal made.
    `unknown` means detached and is deliberately not a fault: 6 of 43 volumes read it here,
    including the intentionally scaled-to-zero game servers.

    The two longhorn-manager pods report DISJOINT volume subsets (43 volumes total across both,
    not 43 each), so offenders are deduped by name rather than counted — a raw count would
    change meaning the moment a volume moved between managers.

    An absent metric is treated as a breach, NOT as green. If the longhorn scrape job dies the
    degraded selector returns empty for exactly the same reason a healthy cluster does, and a
    check that cannot distinguish "nothing is wrong" from "I cannot see" is the failure mode
    this estate keeps rediscovering (manifest-prune's unreadable staged dirs, the backup
    reaper's unpopulated owner map). The volume count doubles as that input assertion: the
    one-hot shape guarantees a `state="healthy"` series per volume even when its value is 0.
    """
    volumes = bridge_io.prom_scalar(
        'count(longhorn_volume_robustness{state="healthy"})'
    )
    if not volumes:
        bridge_streaks._down_streaks["longhorn"], ok, msg = bridge_streaks.down_streak(
            bridge_streaks._down_streaks.get("longhorn", 0),
            cfg.LONGHORN_CONSECUTIVE,
            "no longhorn_volume_robustness series — replica redundancy is UNMONITORED "
            "(job=longhorn scrape down?), which is not the same as healthy",
            "scrape gap grace",
        )
        return ok, msg
    worst = {}
    for labels, _value in bridge_io.prom_vector(
        'longhorn_volume_robustness{state=~"degraded|faulted"} == 1'
    ):
        name = labels.get("pvc") or labels.get("volume", "?")
        state = labels.get("state", "?")
        # faulted outranks degraded if both ever report for one volume
        if worst.get(name) != "faulted":
            worst[name] = state
    if not worst:
        bridge_streaks._down_streaks["longhorn"] = 0
        return True, "%d volume(s) redundant, none degraded or faulted" % int(volumes)
    faulted = sorted(n for n, s in worst.items() if s == "faulted")
    degraded = sorted(n for n, s in worst.items() if s != "faulted")
    parts = []
    if faulted:
        parts.append("%d faulted (%s)" % (len(faulted), ", ".join(faulted[:5])))
    if degraded:
        parts.append(
            "%d degraded, single-copy (%s)" % (len(degraded), ", ".join(degraded[:5]))
        )
    bridge_streaks._down_streaks["longhorn"], ok, msg = bridge_streaks.down_streak(
        bridge_streaks._down_streaks.get("longhorn", 0),
        cfg.LONGHORN_CONSECUTIVE,
        "of %d volume(s): %s" % (int(volumes), "; ".join(parts)),
        "drain/reboot grace",
    )
    return ok, msg


def check_pvc_fullness():
    """Filesystem fullness of every PersistentVolumeClaim the kubelet reports stats for.

    Nothing else covered this. check_disk iterates DISK_MOUNTPOINTS — `/`, `/boot`, `/boot/efi`
    — which are host filesystems, and check_longhorn_volumes reads longhorn_volume_robustness,
    which is replica redundancy rather than space. A Longhorn PVC has its own filesystem at a
    fixed capacity, so a 2 Gi claim can reach 100% while both hosts report hundreds of GB free:
    the app starts failing writes and every existing monitor stays green.

    Reads the CLUSTER Prometheus like check_k8s_workloads, so it belongs to CLUSTER_DEPENDENT
    rather than PROM_DEPENDENT — the gate has to be the one watching this check's own source.

    `max by (namespace, persistentvolumeclaim)` — not `sum` or `avg` — because daniel-box's
    claims are scraped TWICE. k3s serves the kubelet's metric registry on the supervisor's
    /metrics as well, so the same series arrives under job="kubernetes-kubelet" and under
    job="kubernetes-apiserver" (measured 2026-09-01: 43 + 27 = 70 series over 43 claims).
    `max` of two copies of one ratio is that ratio, so the double scrape is harmless; `sum`
    would report a double-scraped claim at twice its real fullness and `avg` would silently
    change meaning the day one job's coverage moved. Grouping is also what makes the count
    below a claim census rather than a scrape-job artifact.
    """
    if not cfg.CLUSTER_PROM_URL:
        return True, "PVC fullness check disabled (no CLUSTER_PROMETHEUS_URL)"
    claims = bridge_io.prom_scalar(
        "count(count by (namespace, persistentvolumeclaim)"
        " (kubelet_volume_stats_capacity_bytes))",
        base=cfg.CLUSTER_PROM_URL,
        source="cluster prometheus",
    )
    vec = bridge_io.prom_vector(
        "max by (namespace, persistentvolumeclaim) (100 *"
        " (1 - kubelet_volume_stats_available_bytes"
        " / kubelet_volume_stats_capacity_bytes))",
        base=cfg.CLUSTER_PROM_URL,
        source="cluster prometheus",
    )
    watched = [
        (labels.get("persistentvolumeclaim", "?"), labels.get("namespace", "?"), pct)
        for labels, pct in vec
        if labels.get("persistentvolumeclaim") not in cfg.PVC_EXCLUDE
    ]
    if not watched:
        # A DIFFERENT fault from a thin claim census below, and it must not reach the `worst`
        # report: the ratio query returned nothing at all, which looks exactly like "no claim is
        # full" and is not the same fact.
        bridge_streaks._down_streaks["pvc_fullness"], ok, msg = (
            bridge_streaks.down_streak(
                bridge_streaks._down_streaks.get("pvc_fullness", 0),
                cfg.PVC_CLAIMS_CONSECUTIVE,
                "no PVC reported a fullness ratio — PVC fullness is UNKNOWN, not OK",
                "kubelet scrape gap grace",
            )
        )
        return ok, msg
    # Fullest first, so a truncated message names the claims closest to failing.
    breaching = [
        "%s/%s %.0f%%" % (ns, pvc, pct)
        for pvc, ns, pct in sorted(watched, key=lambda w: w[2], reverse=True)
        if pct > cfg.PVC_MAX_PCT
    ]
    # The floor is the input assertion, and it is evaluated over ALL claims including the
    # excluded ones: it asserts the metric family is being scraped, which is a different
    # question from which claims this arm judges.
    shortfall = None
    if claims is None or claims < cfg.PVC_MIN_CLAIMS:
        seen = "no" if claims is None else "only %d" % int(claims)
        bridge_streaks._down_streaks["pvc_fullness"], short_ok, short_msg = (
            bridge_streaks.down_streak(
                bridge_streaks._down_streaks.get("pvc_fullness", 0),
                cfg.PVC_CLAIMS_CONSECUTIVE,
                "%s kubelet_volume_stats claims visible, below the floor of %d — PVC fullness is "
                "UNKNOWN, not OK" % (seen, cfg.PVC_MIN_CLAIMS),
                "kubelet scrape gap grace",
            )
        )
        shortfall = (short_ok, short_msg)
    else:
        bridge_streaks._down_streaks["pvc_fullness"] = 0
    # A claim that IS reporting and IS full outranks a complaint about the ones that are not —
    # same ordering as check_disk, and for the same reason.
    if breaching:
        return False, "PVC over %.0f%%: %s" % (
            cfg.PVC_MAX_PCT,
            ", ".join(breaching[:5]),
        )
    if shortfall is not None:
        return shortfall
    worst = max(watched, key=lambda w: w[2])
    return True, "%d claim(s) under %.0f%%, worst %s/%s %.0f%%" % (
        len(watched),
        cfg.PVC_MAX_PCT,
        worst[1],
        worst[0],
        worst[2],
    )
