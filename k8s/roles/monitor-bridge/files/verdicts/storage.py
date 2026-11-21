"""Storage verdicts for check.py — Backblaze B2 and Cloudflare R2.

These decide; `checks/b2.py` and `checks/r2.py` fetch. Each function takes
its inputs and its thresholds as arguments and reads no module-level config, which is what makes
it safe to live here — see bridge/parsing.py's header for the rule and why breaking it fails
silently rather than loudly.

The three R2 action-class frozensets below are the exception that proves the rule: they are
POLICY, not configuration. Cloudflare's pricing page decides which operations bill as Class A and
which as Class B, no env var names them, and `r2_classify_operations` is their only reader. They
stay out of bridge/config.py — the env-reading module — where a reader looking for the billing
rules would have no reason to look and a reader of config.py could not tell them from a
tunable.
"""

from datetime import datetime, timezone

from bridge.types import CheckResult

# Cloudflare's published Class A operations — the expensive, 1M/month arm. From the R2 pricing
# page; an actionType in NEITHER list is counted here too, deliberately (see
# r2_classify_operations).
R2_CLASS_A_ACTIONS = frozenset(
    {
        "ListBuckets",
        "PutBucket",
        "ListObjects",
        "PutObject",
        "CopyObject",
        "CompleteMultipartUpload",
        "CreateMultipartUpload",
        "ListMultipartUploads",
        "UploadPart",
        "UploadPartCopy",
        "ListParts",
        "PutBucketEncryption",
        "PutBucketCors",
        "PutBucketLifecycleConfiguration",
        "LifecycleStorageTierTransition",
    }
)

# Class B — the cheaper, 10M/month arm.
R2_CLASS_B_ACTIONS = frozenset(
    {
        "HeadBucket",
        "HeadObject",
        "GetObject",
        "UsageSummary",
        "GetBucketEncryption",
        "GetBucketLocation",
        "GetBucketCors",
        "GetBucketLifecycleConfiguration",
    }
)

# Free operations — billed on neither arm, so counting them either way would report headroom
# that has not been spent.
R2_FREE_ACTIONS = frozenset({"DeleteObject", "DeleteBucket", "AbortMultipartUpload"})


def b2_sum_versions(pages: list[dict]) -> tuple[int, int]:
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


def b2_storage_verdict(
    used_bytes: float,
    versions: int,
    truncated: bool,
    cap: float,
    max_pct: float,
    max_pages: int,
) -> tuple[bool, str]:
    """(ok, msg) for B2 storage headroom against the free-tier cap.

    Args:
      used_bytes: Bytes summed across every file version in the bucket.
      versions: How many file versions that sum covered.
      truncated: Whether the listing hit its page cap before the cursor cleared.
      cap: The free-tier byte allowance. 0 or absent means "not configured", reported as a fault.
      max_pct: The percentage of `cap` this check tolerates.
      max_pages: The page cap the walk stopped at, named in the truncation message.
    """
    if not cap:
        return CheckResult(False, "B2 storage cap not configured", ["cap unset"])
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
        return CheckResult(
            False,
            "B2 storage listing truncated at %d pages — %s is a FLOOR, not the total"
            % (max_pages, detail),
            ["listing truncated"],
        )
    if pct > max_pct:
        return CheckResult(
            False,
            "B2 storage over %.0f%%: %s" % (max_pct, detail),
            ["storage over %.0f%%" % max_pct],
        )
    return True, "B2 storage %s" % detail


def r2_month_start(now: float) -> datetime:
    """UTC midnight on the 1st of the calendar month containing `now` (epoch seconds).

    R2's free tier resets on the calendar month, so month-to-date is the only window whose
    percentages mean anything — a rolling 30d window would report headroom that does not exist
    on the 2nd and headroom that has already been given back on the 30th.

    Returns an OFFSET-AWARE datetime in UTC. checks/r2.py formats it with a trailing `Z` into the
    GraphQL filter, and a naive local datetime formatted that way would claim to be UTC while
    being five or six hours off — silently querying the wrong window at every month boundary.
    """
    d = datetime.fromtimestamp(now, timezone.utc)
    return d.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def r2_classify_operations(rows: list[dict]) -> tuple[float, float, list[str]]:
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
        if action in R2_CLASS_B_ACTIONS:
            class_b += requests
        elif action in R2_FREE_ACTIONS:
            continue
        elif action in R2_CLASS_A_ACTIONS:
            class_a += requests
        else:
            class_a += requests
            unknown[action] = unknown.get(action, 0) + requests
    return class_a, class_b, sorted(unknown)


def _pct(used: float, limit: float) -> float | None:
    """Percent of `limit` used, or None when the limit is disabled (<= 0)."""
    if limit <= 0:
        return None
    return 100.0 * used / limit


def r2_usage_verdict(
    storage_bytes: float,
    uploads: float,
    class_a: float,
    class_b: float,
    unknown_actions: list[str],
    storage_max_gb: float,
    class_a_max: float,
    class_b_max: float,
    uploads_max: float,
    max_pct: float,
) -> tuple[bool, str]:
    """(ok, msg) from month-to-date R2 usage against the free-tier limits.

    Reports all three arms every cycle whether or not any breaches, so the check message carries
    the trend and not just the alarm — the point of the monitor is to see a runaway client early.

    Args:
      storage_bytes: Peak month-to-date stored bytes, payload plus metadata.
      uploads: Outstanding incomplete multipart uploads; they bill as stored bytes.
      class_a: Month-to-date Class A operation count.
      class_b: Month-to-date Class B operation count.
      unknown_actions: actionTypes counted as Class A because neither list names them.
      storage_max_gb: Free-tier storage allowance in decimal GB. <= 0 disables the arm.
      class_a_max: Free-tier Class A operation allowance. <= 0 disables the arm.
      class_b_max: Free-tier Class B operation allowance. <= 0 disables the arm.
      uploads_max: Tolerated incomplete multipart uploads. <= 0 disables the arm.
      max_pct: The percentage of any limit at which this goes down.
    """
    storage_gb = storage_bytes / 1e9  # R2 bills decimal GB, not GiB
    arms = (
        ("storage", storage_gb, storage_max_gb, "%.2f/%.0f GB"),
        ("Class A", class_a, class_a_max, "%.0f/%.0f"),
        ("Class B", class_b, class_b_max, "%.0f/%.0f"),
    )
    parts = []
    breaching, members = [], []
    for label, used, limit, fmt in arms:
        pct = _pct(used, limit)
        if pct is None:
            parts.append("%s %s (no limit set)" % (label, fmt % (used, limit)))
            continue
        parts.append("%s %s (%.0f%%)" % (label, fmt % (used, limit), pct))
        if pct >= max_pct:
            breaching.append("%s at %.0f%%" % (label, pct))
            members.append(label)

    if uploads_max > 0 and uploads > uploads_max:
        breaching.append(
            "%d incomplete multipart uploads (they bill as storage and do not show in a "
            "listing — check the bucket's AbortIncompleteMultipartUpload lifecycle rule)"
            % uploads
        )
        members.append("multipart uploads")

    msg = "R2 month-to-date: " + ", ".join(parts)
    if unknown_actions:
        msg += " [unclassified ops counted as Class A: %s]" % ", ".join(unknown_actions)
    if breaching:
        return CheckResult(
            False,
            "over %.0f%% of free tier — %s. %s" % (max_pct, "; ".join(breaching), msg),
            members,
        )
    return True, msg


# How old a weekly backup gets before its prune deletes it, which the B2 lock must stay under
# (ADR-0041): `k3s_longhorn_weekly_backup_retain` weekly backups. test_check_bucket_locks.py
# derives it from that default.
B2_PRUNE_AGE_DAYS = 14
_PERIOD_DAYS = {"days": 1, "years": 365}


def b2_lock_verdict(bucket: dict, capabilities: list[str]) -> tuple[bool, str]:
    """(ok, msg) for Object Lock on the B2 bucket Longhorn's weekly tier writes to.

    ADR-0041 records the choice: Object Lock on, a governance-mode default retention shorter than
    `B2_PRUNE_AGE_DAYS`, and a writer key that cannot bypass governance but can extend a
    retention, which `scripts/backup/b2_extend_retention.py` needs to keep old blocks locked.
    Compliance mode is flagged because no key can shorten it.

    Args:
      bucket: One entry of a `b2_list_buckets` response.
      capabilities: The capabilities `b2_authorize_account` granted the key, which is the key
        Longhorn writes with.
    """
    name = bucket.get("bucketName") or "the bucket"
    lock = bucket.get("fileLockConfiguration")
    if not isinstance(lock, dict):
        return CheckResult(
            False,
            "B2 returned no fileLockConfiguration for %s" % name,
            ["no lock config"],
        )
    if lock.get("isClientAuthorizedToRead") is not True:
        return CheckResult(
            False,
            "the key has no readBucketRetentions, so %s's Object Lock is unreadable"
            % name,
            ["lock unreadable"],
        )
    value = lock.get("value") or {}
    if value.get("isFileLockEnabled") is not True:
        return CheckResult(False, "Object Lock is off on %s" % name, ["lock off"])
    retention = value.get("defaultRetention") or {}
    mode = retention.get("mode")
    period = retention.get("period") or {}
    duration = period.get("duration")
    if mode not in ("governance", "compliance") or not duration:
        return CheckResult(
            False,
            "%s has no default retention, so new uploads are not locked" % name,
            ["no default retention"],
        )
    if mode == "compliance":
        return CheckResult(
            False,
            "%s retains in compliance mode, which no key can shorten; ADR-0041 picks governance"
            % name,
            ["compliance mode"],
        )
    unit_days = _PERIOD_DAYS.get(str(period.get("unit")))
    if (
        not isinstance(duration, int)
        or unit_days is None
        or duration * unit_days >= B2_PRUNE_AGE_DAYS
    ):
        return CheckResult(
            False,
            "%s retains for %s %s, not less than the %d days the weekly prune waits"
            % (name, duration, period.get("unit"), B2_PRUNE_AGE_DAYS),
            ["retention outlasts the prune"],
        )
    if "bypassGovernance" in capabilities:
        return CheckResult(
            False,
            "the key Longhorn writes with holds bypassGovernance, so governance mode does "
            "not bind it",
            ["key bypasses governance"],
        )
    if "writeFileRetentions" not in capabilities:
        return CheckResult(
            False,
            "the key Longhorn writes with lacks writeFileRetentions, so the daily sweep cannot "
            "renew the lock on blocks older than the default retention",
            ["key cannot extend retention"],
        )
    return True, "Object Lock on %s: %s, %s %s" % (
        name,
        mode,
        period.get("duration"),
        period.get("unit"),
    )
