#!/usr/bin/env python3
"""Extend the Object Lock retention of every Longhorn object in the B2 weekly bucket.

WHY THIS EXISTS. B2 counts a version's retention from its upload, and a Longhorn backup uploads
only the blocks that changed. The base blocks every retained weekly backup still references are
therefore months old, and the bucket's 7-day default lock no longer covers them. This sweep
sets each current object's retention to now plus the bucket's default period, once a day, so
every block a retained backup references stays locked (ADR-0041, *Consequences*).

WHY NOT FULL BACKUPS. Longhorn's `full-backup-interval` re-uploads every block, which renews
its lock too, but the re-upload is a new version of a content-addressed name. The version it
replaces stays billed until the lifecycle rule removes it, so the bucket would carry about two
copies of the referenced set against B2's 10 GB free tier.

WHY A PRUNE STILL WORKS. Longhorn deletes with a version-less `DeleteObject`, which writes a
hide marker that B2 allows over a locked version. A pruned object drops out of the next
listing, so the sweep stops extending it and its lock lapses within one period.

WHAT IT COSTS. One authorize, one `b2_list_buckets` and one `b2_list_file_names` page per 1,000
objects, all Class C: about four a day. Each `b2_update_file_retention` is Class A, which B2
does not meter.

EXIT CODES. 0 after a sweep or a decline, 1 when B2 refused an update, 75 when the credentials,
the authorize, the bucket read or the listing failed and nothing was extended. Checks 9 and 10
of longhorn-backup-health read the summary line, not the code.

WHEN IT DECLINES. It changes nothing, prints a `declined:` line and exits 0 while the bucket's
lock is off, its default retention is not governance mode, or the key lacks
`writeFileRetentions`. monitor-bridge's `b2_object_lock` row reports each of those states.

Usage (the homelab-b2-retention timer runs it from the primary checkout):
    uv run python scripts/backup/b2_extend_retention.py
"""

import sys
import time
from datetime import datetime, timezone

from diagnostics.probe_lib import core
from lib.b2 import B2Error, B2Session
from lib.cli_help import ArgumentParser
from lib.exit_codes import FAILED, OK, TEMP_FAIL

BACKUPSTORE_PREFIX = "longhorn/backupstore/volumes/"
# A backup's lock file lives seconds; locking its version only keeps a hidden marker billed.
LOCKS_SEGMENT = "/locks/"
CAPABILITY = "writeFileRetentions"
_PERIOD_DAYS = {"days": 1, "years": 365}
# B2's error code for a version that a prune removed between the listing and the update.
FILE_NOT_PRESENT = "file_not_present"
TAG = "b2-retention"


def governance_period_days(bucket: dict) -> tuple[int | None, str]:
    """The bucket's governance default retention in days, or None and the reason it has none.

    Args:
        bucket: One entry of a `b2_list_buckets` response.
    """
    lock = bucket.get("fileLockConfiguration") or {}
    if lock.get("isClientAuthorizedToRead") is not True:
        return None, "the key cannot read the bucket's Object Lock setting"
    value = lock.get("value") or {}
    if value.get("isFileLockEnabled") is not True:
        return None, "Object Lock is off on the bucket"
    retention = value.get("defaultRetention") or {}
    if retention.get("mode") != "governance":
        return None, "the bucket's default retention is not governance mode"
    period = retention.get("period") or {}
    duration = period.get("duration")
    unit_days = _PERIOD_DAYS.get(str(period.get("unit")))
    if not isinstance(duration, int) or duration <= 0 or unit_days is None:
        return None, "the bucket's default retention has no readable period"
    return duration * unit_days, ""


def lockable(files: list[dict]) -> list[dict]:
    """The listed objects whose retention the sweep extends: all but backup lock files."""
    return [f for f in files if LOCKS_SEGMENT not in f.get("fileName", "")]


def extend(
    session: B2Session, files: list[dict], until_ms: int
) -> tuple[int, int, list[str]]:
    """Set each file's governance retention to `until_ms`.

    Returns:
        (extended, gone, failures): the count updated, the count a prune removed after the
        listing, and one `name: error` line per other refusal.
    """
    extended = gone = 0
    failures = []
    for f in files:
        try:
            session.set_retention(f["fileName"], f["fileId"], "governance", until_ms)
        except B2Error as exc:
            if exc.code == FILE_NOT_PRESENT:
                gone += 1
            else:
                failures.append(f"{f['fileName']}: {exc}")
            continue
        extended += 1
    return extended, gone, failures


def run(session: B2Session, now_s: float) -> tuple[int, str]:
    """One sweep over an authorized session. Returns (exit code, summary line)."""
    if CAPABILITY not in session.capabilities:
        return OK, f"{TAG}: declined: the key lacks {CAPABILITY}"
    days, reason = governance_period_days(session.bucket())
    if days is None:
        return OK, f"{TAG}: declined: {reason}"
    files = lockable(session.list_files(BACKUPSTORE_PREFIX))
    until_s = int(now_s) + days * 86400
    extended, gone, failures = extend(session, files, until_s * 1000)
    until = datetime.fromtimestamp(until_s, timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    line = (
        f"{TAG}: extended {extended} of {len(files)} objects to {until}, "
        f"{gone} gone, {session.class_c} Class C"
    )
    if failures:
        return FAILED, f"{line}; FAILED {len(failures)}: {failures[0]}"
    return OK, line


def main(
    argv: list[str] | None = None,
    *,
    _secret=core.sops_extract,
    _session=B2Session,
    _now=time.time,
) -> int:
    """Run one sweep. Exits 0 on a sweep or a decline, 1 on a refused update, 75 on an error."""
    ArgumentParser(description=__doc__.split("\n", 1)[0]).parse_args(argv)
    try:
        session = _session(
            _secret("longhorn_b2_key_id"), _secret("longhorn_b2_application_key")
        )
        if not session.bucket_id:
            session.lookup_bucket(core.longhorn_b2_bucket())
        code, line = run(session, _now())
    except (B2Error, core.Unreadable) as exc:
        print(f"{TAG}: error: {exc}", file=sys.stderr)
        return TEMP_FAIL
    print(line)
    return code


if __name__ == "__main__":
    sys.exit(main())
