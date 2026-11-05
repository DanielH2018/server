"""Check 12 of the Longhorn backup-plane heartbeat: k3s's native etcd snapshots still reach S3.

Same contract as longhorn_backup_health_logic.py: stdlib only, with every clock reading and every
read injected. The reader runs under `uv run --no-project`, so the drop-in is parsed here, line by
line, rather than with a YAML library.

The bound derives from `etcd-snapshot-schedule-cron` in the installed drop-in, read each run, so a
schedule change in the `homelab-daniel-box` package moves the bound with no `setup/k3s` apply.
*The token comparison stays on daniel-box* and *Check 12* in
docs/target-architecture/host/h2-shrink-host-plane.md have the design.
"""

from __future__ import annotations

import re
from collections.abc import Callable

# Resolves through the same sys.path setup as the sibling logic modules: host_lib.py sits beside
# this package on a host, and under `hosts/lib` in the checkout.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[3] / "hosts" / "lib"))

from host_lib import rfc3339_to_epoch

# ETCDSnapshotFile is cluster-scoped, so the namespace the reader's runner adds changes nothing.
LIST_ARGS = ("get", "etcdsnapshotfiles.k3s.cattle.io", "-o", "json")

ARMED_KEY = "etcd-s3"
SCHEDULE_KEY = "etcd-snapshot-schedule-cron"

# A stale off-box copy is a backup-plane fault, so it ranks with check 2's stale backup.
_RANK = 1
_SLACK_S = 2 * 3600
_KEY_VALUE_RE = re.compile(r"^(?P<key>[A-Za-z0-9_-]+):\s*(?P<value>.*?)\s*$")
_EVERY_N_HOURS_RE = re.compile(r"^\d+ \*/(?P<hours>\d+) \* \* \*$")
_ONE_HOUR_RE = re.compile(r"^\d+ \d+ \* \* \*$")


def parse_drop_in(text: str) -> dict[str, str]:
    """The drop-in's top-level `key: scalar` lines, with surrounding quotes removed.

    An indented line or a list item is skipped, which is all this check needs: both keys it reads
    are top-level scalars.
    """
    settings = {}
    for line in text.splitlines():
        match = _KEY_VALUE_RE.match(line)
        if match:
            settings[match["key"]] = match["value"].strip("\"'")
    return settings


def schedule_period_s(schedule: str) -> int | None:
    """Seconds between two runs of a `MINUTE */N * * *` or `MINUTE HOUR * * *` schedule.

    A step that does not divide 24 runs closer together across midnight, so N hours is still the
    longest gap. Returns None for any other shape, which check 12 reports rather than guessing a
    bound.
    """
    normal = " ".join(schedule.split())
    every_n = _EVERY_N_HOURS_RE.match(normal)
    if every_n:
        hours = int(every_n["hours"])
        return hours * 3600 if 1 <= hours <= 24 else None
    return 86400 if _ONE_HOUR_RE.match(normal) else None


def allowed_age_s(read_drop_in: Callable[[], str]) -> int | tuple[int, str]:
    """The oldest an S3 snapshot may be in seconds, or the problem that stops this check judging one.

    Args:
        read_drop_in: returns the installed drop-in's text; an OSError is the fault to report.

    Returns:
        The bound when the upload is armed and the schedule parses, else a ranked problem.
    """
    try:
        settings = parse_drop_in(read_drop_in())
    except FileNotFoundError, NotADirectoryError:
        return _RANK, "etcd snapshot drop-in missing — cannot judge S3 uploads"
    except (OSError, UnicodeError) as err:
        return _RANK, f"etcd snapshot drop-in unreadable: {err}"
    if ARMED_KEY not in settings or SCHEDULE_KEY not in settings:
        missing = ARMED_KEY if ARMED_KEY not in settings else SCHEDULE_KEY
        return _RANK, f"etcd snapshot drop-in has no {missing} key"
    if settings[ARMED_KEY].lower() != "true":
        return (
            _RANK,
            f"off-box etcd snapshots are disarmed ({ARMED_KEY}: {settings[ARMED_KEY]})",
        )
    period_s = schedule_period_s(settings[SCHEDULE_KEY])
    if period_s is None:
        return (
            _RANK,
            f"cannot derive an S3 snapshot age bound from {SCHEDULE_KEY}: "
            f"{settings[SCHEDULE_KEY]!r}",
        )
    return period_s + _SLACK_S


def _s3_snapshot_time(item: dict) -> float | None:
    """When a usable S3 snapshot was taken; None for a local, failed or undated entry.

    An entry whose `readyToUse` is not true must not read as a fresh backup.
    """
    spec = item.get("spec") or {}
    status = item.get("status") or {}
    location = str(spec.get("location") or "")
    if not location.startswith("s3://") or status.get("readyToUse") is not True:
        return None
    return rfc3339_to_epoch(str(status.get("creationTime") or ""))


def check_newest_snapshot(
    items: list[dict], allowed_s: int, now_s: float
) -> tuple[tuple[int, str] | None, float]:
    """Returns (problem, age_s) for the newest usable S3 snapshot among ETCDSnapshotFile items."""
    times = [t for t in map(_s3_snapshot_time, items) if t is not None]
    if not times:
        return (_RANK, "no ready etcd snapshot with an S3 location exists"), 0.0
    age_s = now_s - max(times)
    if age_s > allowed_s:
        return (
            _RANK,
            f"newest S3 etcd snapshot is {int(age_s / 3600)}h old "
            f"(limit {int(allowed_s / 3600)}h from {SCHEDULE_KEY})",
        ), age_s
    return None, age_s
