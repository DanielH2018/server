"""The pi-peer-backup CronJob must not attach its PVC at the minute a Longhorn job runs.

Longhorn's recurring jobs fire in UTC; the CronJob follows `tz` (America/Chicago), so the UTC
minute it lands on moves with DST. Until 2026-09-21 it ran at 23:30 local, chosen to clear the
03:30 UTC daily tick — and under CDT that is 04:30 UTC, the weekly shard's slot. On 2026-09-19
the pod's CSI attach landed 1.5 s before weekly-backup-d6 reached the volume, the recurring job
refused it (`invalid state for recurring job: attaching`), and the shard finished green with the
volume skipped. The "k3s Longhorn Backup" tile went DOWN 30 h later on staleness.

The slots are read from the k3s role, not restated here, so moving a Longhorn cron re-runs this
guard against the new value. Both DST offsets are checked: a slot clear in September is not
clear in December unless both are.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from _helpers import ROLES
from _helpers import load_yaml

PI_PEER_DEFAULTS = ROLES / "k8s/pi-peer-backup/defaults/main.yml"
K3S_DEFAULTS = ROLES / "setup/k3s/defaults/main.yml"
ALL_VARS = ROLES.parent / "inventory/group_vars/all.yml"

# One date under each offset America/Chicago uses.
CDT_DAY = datetime(2026, 9, 19, tzinfo=ZoneInfo("UTC"))
CST_DAY = datetime(2026, 12, 19, tzinfo=ZoneInfo("UTC"))


def _hhmm(cron: str) -> tuple[int, int]:
    minute, hour = cron.split()[:2]
    return int(hour), int(minute)


def _utc_minutes(local_cron: str, tz: str) -> set[tuple[int, int]]:
    """The (hour, minute) UTC slots a `tz`-scheduled cron lands on across the year."""
    hour, minute = _hhmm(local_cron)
    slots = set()
    for day in (CDT_DAY, CST_DAY):
        local = day.astimezone(ZoneInfo(tz)).replace(hour=hour, minute=minute)
        utc = local.astimezone(ZoneInfo("UTC"))
        slots.add((utc.hour, utc.minute))
    return slots


def _longhorn_slots() -> dict[str, tuple[int, int]]:
    """Every Longhorn job that attaches or snapshots a backed-up volume, by UTC slot."""
    k3s = load_yaml(K3S_DEFAULTS)
    return {
        "daily backup": _hhmm(k3s["k3s_longhorn_backup_cron"]),
        "restore drill": _hhmm(k3s["k3s_longhorn_restore_drill_cron"]),
        "weekly shard": _hhmm(k3s["k3s_longhorn_weekly_backup_minute_hour"]),
    }


def _collisions(local_cron: str) -> dict[str, tuple[int, int]]:
    tz = load_yaml(ALL_VARS)["tz"]
    landed = _utc_minutes(local_cron, tz)
    return {name: slot for name, slot in _longhorn_slots().items() if slot in landed}


def test_the_deployed_schedule_is_clear_under_both_offsets() -> None:
    """CLEAN half: the value in defaults lands on no Longhorn slot in either DST state."""
    schedule = load_yaml(PI_PEER_DEFAULTS)["pi_peer_backup_k8s_schedule"]
    assert _collisions(schedule) == {}, (
        f"pi_peer_backup_k8s_schedule {schedule!r} attaches the PVC while a Longhorn job "
        "runs; Longhorn refuses a volume in state `attaching` and skips it for the week"
    )


def test_the_slot_that_lost_a_weekly_backup_is_flagged() -> None:
    """FLAGGED half: 23:30 local, the 2026-09-19 collision, must still be caught."""
    assert _collisions("30 23 * * *") == {"weekly shard": (4, 30)}
