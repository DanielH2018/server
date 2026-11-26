"""Alertmanager's `weekly-reboot` mute covers the minutes the reboot schedule implies.

The mute derives from the reboot's `weekly_reboot_*` values and the lead and recovery allowance
in group_vars/all.yml. This module holds the rendered mute to `tests/_reboot_mute.py`'s sum of
those values, at the committed schedule and at schedules whose window crosses midnight in
either direction.

Run: uv run pytest tests/services/alertmanager/test_alertmanager_reboot_mute.py
"""

import pytest
from _reboot_mute import mute_minutes, window_minutes


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
    # fact: k8s/roles/observability/CLAUDE.md#Alert plane
    mute = mute_minutes(**overrides)
    assert mute == window_minutes(**overrides)
    assert mute != mute_minutes()


def test_a_mute_that_reads_a_different_schedule_is_flagged():
    """The rejecting half: a mute moved without the reboot no longer covers the window."""
    assert mute_minutes(weekly_reboot_hour=3) != window_minutes()
