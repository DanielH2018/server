"""Alertmanager's `weekly-reboot` mute covers the minutes the reboot schedule implies.

The mute derives from the reboot's `weekly_reboot_*` values and the lead and recovery allowance
in group_vars/all.yml. This module computes the same window from those values alone, at the
committed schedule and at schedules whose window crosses midnight in either direction.

Run: uv run pytest ansible/tests/services/test_alertmanager_reboot_mute.py
"""

import pytest
from lib import yaml_fast
from _k8s_render import render_role_template

from lib.repo_paths import ALL_VARS

GROUP_VARS = yaml_fast.safe_load(ALL_VARS.read_text())
MINUTES_PER_DAY = 24 * 60
MINUTES_PER_WEEK = 7 * MINUTES_PER_DAY
WEEKDAYS = [
    "sunday",
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
]


def _clock(value: str) -> int:
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


def mute_minutes(**overrides) -> set[int]:
    """The minutes of the week, from Sunday 00:00, that the rendered `weekly-reboot` mutes."""
    secret = yaml_fast.safe_load(
        render_role_template("observability", "alertmanager-secret.yaml.j2", overrides)
    )
    config = yaml_fast.safe_load(secret["stringData"]["alertmanager.yml"])
    [interval] = [i for i in config["time_intervals"] if i["name"] == "weekly-reboot"]
    minutes = set()
    for entry in interval["time_intervals"]:
        assert entry["location"] == GROUP_VARS["weekly_reboot_timezone"], entry
        for day in entry["weekdays"]:
            for span in entry["times"]:
                start = WEEKDAYS.index(day) * MINUTES_PER_DAY
                minutes |= set(
                    range(
                        start + _clock(span["start_time"]),
                        start + _clock(span["end_time"]),
                    )
                )
    return minutes


def window_minutes(**overrides) -> set[int]:
    """The minutes of the week the schedule implies: lead before the cron, delay and recovery after."""
    values = {**GROUP_VARS, **overrides}
    lead = int(values["weekly_reboot_mute_lead_minutes"])
    opens = (
        int(values["weekly_reboot_weekday"]) * MINUTES_PER_DAY
        + int(values["weekly_reboot_hour"]) * 60
        + int(values["weekly_reboot_minute"])
        - lead
    )
    duration = (
        lead
        + int(values["weekly_reboot_shutdown_delay_minutes"])
        + int(values["weekly_reboot_mute_recovery_minutes"])
    )
    return {(opens + offset) % MINUTES_PER_WEEK for offset in range(duration)}


def test_the_mute_covers_the_window_at_the_committed_schedule():
    """Sunday 07:25 to 08:15, the window both plans name."""
    mute = mute_minutes()
    assert mute == window_minutes()
    assert (min(mute), max(mute) + 1) == (7 * 60 + 25, 8 * 60 + 15)


@pytest.mark.parametrize(
    "overrides",
    [
        {"weekly_reboot_hour": 3, "weekly_reboot_minute": 10},
        {"weekly_reboot_shutdown_delay_minutes": 2},
        {"weekly_reboot_mute_recovery_minutes": 10},
        # The lead crosses midnight backwards, into Saturday.
        {"weekly_reboot_hour": 0, "weekly_reboot_minute": 2},
        # The recovery crosses midnight forwards, from Saturday into Sunday.
        {
            "weekly_reboot_weekday": 6,
            "weekly_reboot_hour": 23,
            "weekly_reboot_minute": 50,
        },
    ],
    ids=[
        "earlier",
        "shorter-delay",
        "shorter-recovery",
        "lead-crosses",
        "recovery-crosses",
    ],
)
def test_moving_the_reboot_moves_the_mute(overrides):
    # fact: ansible/roles/k8s/observability/CLAUDE.md#Alert plane
    mute = mute_minutes(**overrides)
    assert mute == window_minutes(**overrides)
    assert mute != mute_minutes()


def test_a_mute_that_reads_a_different_schedule_is_flagged():
    """The rejecting half: a mute moved without the reboot no longer covers the window."""
    assert mute_minutes(weekly_reboot_hour=3) != window_minutes()
