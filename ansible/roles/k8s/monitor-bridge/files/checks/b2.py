"""Backblaze B2 checks for monitor-bridge — reachability (the B2_DEPENDENT gate) and storage usage.

Reads config as `cfg.X` and queries B2 through the `src` argument (`bridge.sources.Sources`),
which a test replaces with a fake; `b2_reachable` and `b2_authorize` are patched on THIS module, where
`check_b2_reachable` reads them. The probe caches are `src.state.b2_probe` / `src.state.b2_storage`
(`bridge.streaks.State`), so every `Sources` starts with its own. Rule and enforcement:
bridge/config.py's header.
"""

import base64
import time
import urllib.error
import urllib.parse

from bridge.config import Config
from bridge.sources import Sources
from bridge.types import JsonObject, JsonValue, as_object
from verdicts.storage import b2_storage_verdict, b2_sum_versions


def b2_authorize_data(cfg: Config, src: Sources) -> JsonObject:
    """The parsed b2_authorize_account response. Raises on any transport/HTTP failure.

    The bridge's only authorize call: `b2_authorize` (the gate) and `b2_storage_usage` both
    reach B2 through it.

    DECIDED: the bridge keeps this client rather than using scripts/lib/b2.py's `B2Session`
    (#3766). `b2_reachable` picks its cache TTL from the exception type: `Sources.get_json`
    (over `bridge.net._get_json`) re-raises an HTTPError untouched (B2 answered, so the call
    was billed) and wraps a transport failure as RuntimeError (nothing billed).
    `lib.b2.http_json` folds both into one
    `B2Error`, which would bring back the 2026-08-30 gate that held a recovery DOWN for 25
    minutes. `B2Session` also authorizes against a fixed `AUTHORIZE_URL`, where
    `cfg.B2_PROBE_URL` is kept swappable to a Class C call (bridge/config_io.py). The pod
    cannot import scripts/lib either, since `files/` ships as a ConfigMap; shipping the module
    in would roll the bridge, the alert pipeline itself, on every edit to it.
    """
    token = base64.b64encode(
        ("%s:%s" % (cfg.B2_PROBE_KEY_ID, cfg.B2_PROBE_APPLICATION_KEY)).encode()
    ).decode()
    return as_object(
        src.get_json(cfg.B2_PROBE_URL, headers={"Authorization": "Basic %s" % token}),
        "b2_authorize_account response",
    )


def b2_storage_api(auth: dict) -> tuple[str | None, str | None, str | None]:
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


def b2_storage_usage(
    cfg: Config, src: Sources, now: float | None = None
) -> tuple[bool, str]:
    """Throttled B2 storage-headroom probe. (ok, msg).

    SUCCESSES are cached for B2_STORAGE_INTERVAL_S and a failure is not, the
    EMAIL_PROBE_INTERVAL_S idiom rather than b2_reachable's cache-both: a listing failure is far
    more likely to be a transient 5xx than a cap, and b2_reachable already owns the cap signal, so
    there is no spend spiral to protect against here. Empty credentials -> disabled (stays up).
    """
    if not cfg.B2_PROBE_KEY_ID or not cfg.B2_PROBE_APPLICATION_KEY:
        return True, "B2 storage check disabled (no credentials)"
    now = now if now is not None else time.time()
    if (
        src.state.b2_storage["ok"]
        and now - src.state.b2_storage["ts"] < cfg.B2_STORAGE_INTERVAL_S
    ):
        return src.state.b2_storage["ok"], "%s (checked %.0fh ago)" % (
            src.state.b2_storage["msg"],
            (now - src.state.b2_storage["ts"]) / 3600,
        )
    try:
        api_url, token, bucket_id = b2_storage_api(b2_authorize_data(cfg, src))
        if not api_url or not token:
            raise RuntimeError("B2 auth response carried no storage apiUrl/token")
        if not bucket_id:
            raise RuntimeError(
                "B2 key is not bucket-scoped (no bucketId) — cannot size a bucket"
            )
        pages, truncated = b2_list_versions(cfg, src, api_url, token, bucket_id)
        used, versions = b2_sum_versions(pages)
        ok, msg = b2_storage_verdict(
            used,
            versions,
            truncated,
            cfg.B2_STORAGE_CAP_BYTES,
            cfg.B2_STORAGE_MAX_PCT,
            cfg.B2_STORAGE_MAX_PAGES,
        )
    except Exception as e:
        ok, msg = False, "B2 storage probe failed: %s" % e
    src.state.b2_storage["ts"] = now
    src.state.b2_storage["ok"] = ok
    src.state.b2_storage["msg"] = msg
    return ok, msg


def b2_list_versions(
    cfg: Config, src: Sources, api_url: str, token: str, bucket_id: str
) -> tuple[list[dict], bool]:
    """(pages, truncated) — every b2_list_file_versions page for the bucket.

    Paginates on the (nextFileName, nextFileId) cursor B2 returns; a page with neither is the
    last. Stops at B2_STORAGE_MAX_PAGES and says so, rather than looping on a cursor that never
    clears.
    """
    pages: list[JsonObject] = []
    start_name: JsonValue = None
    start_id: JsonValue = None
    for _ in range(cfg.B2_STORAGE_MAX_PAGES):
        payload: dict[str, JsonValue] = {"bucketId": bucket_id, "maxFileCount": 1000}
        if start_name:
            payload["startFileName"] = start_name
        if start_id:
            payload["startFileId"] = start_id
        page = as_object(
            src.post_json(
                "%s/b2api/v3/b2_list_file_versions" % api_url.rstrip("/"),
                payload,
                headers={"Authorization": token},
            ),
            "b2_list_file_versions response",
        )
        pages.append(page)
        start_name = page.get("nextFileName")
        start_id = page.get("nextFileId")
        if not start_name and not start_id:
            return pages, False
    return pages, True


def check_b2_storage(cfg: Config, src: Sources) -> tuple[bool, str]:
    return b2_storage_usage(cfg, src)


def b2_authorize(cfg: Config, src: Sources) -> tuple[bool, str]:
    """Authenticate against B2. (ok, msg) — the msg carries B2's own error text on failure.

    Basic auth with the key id + application key is the whole protocol for b2_authorize_account,
    sent by b2_authorize_data. _get_json re-raises HTTPError with the response body appended, so a cap breach arrives here as
    "HTTP Error 403: ... transaction_cap_exceeded ..." and that string is what reaches Kuma and
    Discord — the named cause G3 asked for.
    """
    data = b2_authorize_data(cfg, src)
    # A 200 from something that isn't B2 must not read as healthy. Accept EITHER field rather than
    # pinning the response shape: Backblaze publishes a body example for v4 (accountId top-level)
    # but not for v3, whose documented change was to group endpoint info under `apiInfo`. Both
    # fields have been present since v1, so this survives a version bump either way — and a wrong
    # guess here would page every cycle rather than fail safe.
    if not (data.get("accountId") or data.get("authorizationToken")):
        return False, "B2 auth returned neither accountId nor authorizationToken"
    return True, "B2 reachable"


def b2_reachable(
    cfg: Config, src: Sources, now: float | None = None
) -> tuple[bool, str]:
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

    The cache is `src.state.b2_probe`, reset on container restart like the streak counters.
    """
    if not cfg.B2_PROBE_KEY_ID or not cfg.B2_PROBE_APPLICATION_KEY:
        return True, "B2 reachability check disabled (no credentials)"
    now = now if now is not None else time.time()
    if now - src.state.b2_probe["ts"] < src.state.b2_probe["ttl"]:
        return src.state.b2_probe["ok"], "%s (checked %.0fm ago)" % (
            src.state.b2_probe["msg"],
            (now - src.state.b2_probe["ts"]) / 60,
        )
    try:
        ok, msg = b2_authorize(cfg, src)
        ttl = cfg.B2_PROBE_INTERVAL_S
    except urllib.error.HTTPError as e:
        # B2 answered, so the call was billed — hold the full interval.
        ok, msg, ttl = False, "B2 unreachable: %s" % e, cfg.B2_PROBE_INTERVAL_S
    except Exception as e:
        # Never reached B2, so nothing was billed — retry on the next cycle.
        ok, msg, ttl = False, "B2 unreachable: %s" % e, cfg.B2_TRANSPORT_RETRY_S
    src.state.b2_probe["ts"] = now
    src.state.b2_probe["ok"] = ok
    src.state.b2_probe["msg"] = msg
    src.state.b2_probe["ttl"] = ttl
    return ok, msg


def check_b2_reachable(cfg: Config, src: Sources) -> tuple[bool, str]:
    return b2_reachable(cfg, src)
