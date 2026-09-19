"""Guard: the Pi's apt timers are pinned to one daily firing in the quiet window.

Ubuntu's apt-daily.timer fires twice a day with a 12-hour random delay, and on a 456 MB host
that keeps 10-25 MB free each firing is a four-minute, 60 MB burst (#2007). The drop-in
optimize_pi writes has to clear the packaged schedule with an empty `OnCalendar=` before
setting its own -- a drop-in that only adds a line leaves both schedules live, which is
green in every way except the one this exists for.

Run: uv run pytest ansible/tests/setup/test_pi_apt_timers_pinned.py
"""

import re

from lib import yaml_fast
from _helpers import SETUP_ROLES

ROLE = SETUP_ROLES / "optimize_pi"
TASKS = ROLE / "tasks" / "main.yml"
DEFAULTS = ROLE / "defaults" / "main.yml"
TIMERS_VAR = "optimize_pi_apt_timers"
# The census must keep finding both: the update half is the burst that was measured, and
# the upgrade half runs an hour later on what it downloaded.
MUST_PIN = frozenset({"apt-daily.timer", "apt-daily-upgrade.timer"})
QUIET_WINDOW = range(3, 6)  # 03:00-05:59 UTC, between the midnight cluster and 06:00


def _dropin_task() -> dict:
    tasks = yaml_fast.safe_load(TASKS.read_text())
    for task in tasks:
        copy = task.get("ansible.builtin.copy") if isinstance(task, dict) else None
        if isinstance(copy, dict) and "quiet-hour.conf" in str(copy.get("dest")):
            return task
    raise AssertionError(
        "optimize_pi no longer writes the apt timer quiet-hour drop-in"
    )


def dropin_problem(content: str) -> str | None:
    """The failure message for a timer drop-in's content, else None."""
    lines = [
        ln.strip()
        for ln in content.splitlines()
        if ln.strip() and not ln.startswith("#")
    ]
    if "[Timer]" not in lines:
        return "no [Timer] section"
    calendars = [ln for ln in lines if ln.startswith("OnCalendar=")]
    if not calendars or calendars[0] != "OnCalendar=":
        return "the first OnCalendar= is not empty, so the packaged schedule stays live too"
    if len(calendars) != 2:
        return (
            f"expected exactly one schedule after the reset, found {len(calendars) - 1}"
        )
    return None


def test_the_dropin_resets_before_it_schedules() -> None:
    task = _dropin_task()
    assert dropin_problem(task["ansible.builtin.copy"]["content"]) is None


def test_a_dropin_that_only_adds_a_schedule_is_flagged() -> None:
    assert dropin_problem("[Timer]\nOnCalendar=*-*-* 04:20\n") == (
        "the first OnCalendar= is not empty, so the packaged schedule stays live too"
    )


def test_both_timers_are_pinned_inside_the_quiet_window() -> None:
    timers = yaml_fast.safe_load(DEFAULTS.read_text()).get(TIMERS_VAR)
    assert isinstance(timers, list), f"{TIMERS_VAR} is not a list in defaults/main.yml"
    units = {t["unit"] for t in timers}
    missing = MUST_PIN - units
    assert not missing, f"{TIMERS_VAR} no longer pins {sorted(missing)}"
    for timer in timers:
        match = re.fullmatch(r"(\d\d):(\d\d)", str(timer["at"]))
        assert match, f"{timer['unit']}: `at` is {timer['at']!r}, not HH:MM"
        assert int(match.group(1)) in QUIET_WINDOW, (
            f"{timer['unit']} is pinned to {timer['at']}, outside the 03:00-05:59 window "
            "the task comment names"
        )
    by_unit = {t["unit"]: t["at"] for t in timers}
    assert by_unit["apt-daily.timer"] < by_unit["apt-daily-upgrade.timer"], (
        "the upgrade timer must fire after the update timer it depends on"
    )
