"""Storage verdicts for check.py — Backblaze B2 and Cloudflare R2.

These decide; `checks/b2.py` and `checks/r2.py` fetch. Each function takes
its inputs and its thresholds as arguments and reads no module-level config, which is what makes
it safe to live here — see bridge/parsing.py's header for the rule and why breaking it fails
silently rather than loudly.

The three R2 action-class frozensets below are the exception that proves the rule: they are
POLICY, not configuration. Cloudflare's pricing page decides which operations bill as Class A and
which as Class B, no env var names them, and `r2_classify_operations` is their only reader. They
sat in bridge/config.py — the env-reading module — until 2026-09-04, where a reader looking for
the billing rules had no reason to look and a reader of config.py had no way to tell them from a
tunable.
"""

from datetime import datetime, timezone

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
            % (max_pages, detail),
        )
    if pct > max_pct:
        return False, "B2 storage over %.0f%%: %s" % (max_pct, detail)
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

    Reports all three arms every cycle whether or not any breaches, so the Kuma message carries
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
