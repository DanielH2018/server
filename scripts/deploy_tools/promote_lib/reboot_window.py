"""The weekly reboot's no-promotion window, for `decide`, `land-wait` and `promote.yml`'s cron.

The reboot kills the soak Job's pod, recreates pods and fires critical rules after T0, and M2's
`weekly-reboot` mute is an Alertmanager route, so it silences nothing `verify` reads. A
promotion verifying across the reboot would therefore hold, and only the operator clears a
hold. So no promotion runs into it (docs/target-architecture/deploy/d5-bulk-move.md, *The
stateful hold, its post, and the reset input*).

THE WINDOW for a target T opens T's derived verify timeout before the reboot, so a promotion
that starts just before it still finishes verifying first, and closes when M2's mute ends:

    reboot = the cron's fire + weekly_reboot_shutdown_delay_minutes
    start  = reboot - T's timeout
    end    = reboot + weekly_reboot_mute_recovery_minutes      (exclusive)

The end is exclusive, so the scheduled run that fires at the end promotes rather than defers.
`weekly_reboot_mute_lead_minutes` opens the mute 10 minutes before the reboot, inside every
window, so it shortens none of them.

THE INPUTS are the reboot cron's own variables in `ansible/inventory/group_vars/all.yml`, read
from the checkout the way `bundle_census.published_tags` reads the inventory, so moving the
reboot moves the window. `promote.yml`'s `schedule:` cron is a literal GitHub reads, so a test
derives it here (`end_cron`) and fails when the two differ.

Run: uv run python scripts/deploy_tools/promote.py decide   (the window is read inside)
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
_sys.path.insert(1, str(_Path(__file__).resolve().parents[1]))
from lib import yaml_fast
from lib.repo_paths import ALL_VARS, REPO

from deploy_tools.promote_lib.prune import ReadError

# The file, relative to the checkout `--repo` names.
SOURCE = ALL_VARS.relative_to(REPO)
# Each integer the window derives from, with its inclusive range.
_INTEGERS = {
    "weekly_reboot_minute": (0, 59),
    "weekly_reboot_hour": (0, 23),
    # Cron day-of-week, 0 = Sunday.
    "weekly_reboot_weekday": (0, 6),
    "weekly_reboot_shutdown_delay_minutes": (0, 24 * 60),
    "weekly_reboot_mute_lead_minutes": (0, 24 * 60),
    "weekly_reboot_mute_recovery_minutes": (0, 24 * 60),
}
TIMEZONE_KEY = "weekly_reboot_timezone"


@dataclass(frozen=True)
class Schedule:
    """The weekly reboot as group_vars/all.yml states it.

    Attributes:
        minute, hour, weekday: the reboot cron's fields, `weekday` 0 for Sunday.
        delay_min: the minutes from the cron's fire to the reboot.
        lead_min: the minutes M2's mute opens before the cron fires.
        recovery_min: the minutes M2's mute stays open after the reboot.
    """

    minute: int
    hour: int
    weekday: int
    delay_min: int
    lead_min: int
    recovery_min: int


def read_schedule(repo: Path) -> Schedule:
    """The schedule in the checkout at `repo`.

    Raises:
        ReadError: the file is unreadable, a key is missing, a value is not an integer in
            range, such as a Jinja string, or the timezone is not UTC. GitHub reads a
            `schedule:` cron in UTC and a fixed cron cannot follow daylight saving.
    """
    path = repo / SOURCE
    try:
        data = yaml_fast.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as err:
        raise ReadError(f"{path}: {err}") from err
    data = data if isinstance(data, dict) else {}
    values = []
    for key, (low, high) in _INTEGERS.items():
        value = data.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ReadError(f"{key} in {path} must be an integer, not {value!r}")
        if not low <= value <= high:
            raise ReadError(f"{key} in {path} is {value}, outside {low}..{high}")
        values.append(value)
    if data.get(TIMEZONE_KEY) != "UTC":
        raise ReadError(
            f"{TIMEZONE_KEY} in {path} is {data.get(TIMEZONE_KEY)!r}; the window and "
            "promote.yml's cron read UTC only"
        )
    return Schedule(*values)


def _fires(schedule: Schedule, now: datetime) -> list[datetime]:
    """The cron's fires in the week before `now`'s, in its week, and in the week after."""
    cron_day = (now.weekday() + 1) % 7  # datetime counts from Monday, cron from Sunday
    back = timedelta(days=(cron_day - schedule.weekday) % 7)
    fire = (now - back).replace(
        hour=schedule.hour, minute=schedule.minute, second=0, microsecond=0
    )
    return [fire + timedelta(weeks=k) for k in (-1, 0, 1)]


def window(
    schedule: Schedule, timeout_s: int, now: datetime
) -> tuple[datetime, datetime] | None:
    """The window `(start, end)` that holds `now`, for a target whose timeout is `timeout_s`.

    None when `now` is outside every window.
    """
    now = now.astimezone(UTC)
    for fire in _fires(schedule, now):
        reboot = fire + timedelta(minutes=schedule.delay_min)
        start = reboot - timedelta(seconds=timeout_s)
        end = reboot + timedelta(minutes=schedule.recovery_min)
        if start <= now < end:
            return start, end
    return None


def end_cron(schedule: Schedule) -> str:
    """The `schedule:` cron, in UTC, that fires when the window ends."""
    fire = _fires(schedule, datetime(2026, 1, 4, tzinfo=UTC))[1]
    end = fire + timedelta(minutes=schedule.delay_min + schedule.recovery_min)
    return f"{end.minute} {end.hour} * * {(end.weekday() + 1) % 7}"


def deferral(
    schedule: Callable[[], Schedule],
    timeout_s: Callable[[], int],
    now: Callable[[], datetime],
) -> str:
    """Why `decide` defers T, or "" when it promotes.

    `timeout_s` is T's own derived verify timeout, from the function `verify` calls, so the
    two never disagree. The clock is read last, after every other read of the run.

    Raises:
        ReadError: the schedule is unreadable, or T's timeout cannot be derived.
    """
    found = schedule()
    try:
        seconds = timeout_s()
    except (ValueError, yaml.YAMLError) as err:
        raise ReadError(f"T's verify timeout: {err}") from err
    held = window(found, seconds, now())
    if held is None:
        return ""
    start, end = held
    return (
        f"inside the weekly reboot's no-promotion window, {start:%a %H:%M} to "
        f"{end:%H:%M} UTC for a {seconds // 60}-minute verify timeout; the scheduled "
        "run at its end promotes master's tip"
    )


def remaining(
    schedule: Schedule, timeout_s: int, now: datetime
) -> tuple[int, datetime] | None:
    """`(seconds until the window ends, its end)` when `now` is inside one, else None.

    `land-wait` reads it with `chained_timeout.VERIFY_CAP_S`, the widest window any T has,
    because it holds no T's bundle, and a T later than its merge may promote it.
    """
    held = window(schedule, timeout_s, now)
    return None if held is None else (int((held[1] - now).total_seconds()), held[1])
