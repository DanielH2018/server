"""Alertmanager's `weekly-reboot` mute covers the same minutes as Kuma's maintenance window.

Both derive from the reboot cron's `weekly_reboot_*` values and the mute's lead and recovery
allowance in group_vars/all.yml. `ansible/roles/k8s/uptime-kuma/tests/test_maintenance_window.py`
holds the cron and Kuma's window to those values. This module holds Alertmanager's interval to
Kuma's window, minute for minute, while Kuma still keeps one: at the committed schedule, and
at schedules whose window crosses midnight in either direction.

Run: uv run pytest ansible/tests/services/test_alertmanager_reboot_mute.py
"""

import importlib.util

import pytest
from lib import yaml_fast
from _k8s_render import render_role_template

from lib.repo_paths import ALL_VARS, K8S_ROLES

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

# Another role's files/ is not on sys.path here, so load Kuma's derivation by path.
_spec = importlib.util.spec_from_file_location(
    "kuma_render_maintenance",
    K8S_ROLES / "uptime-kuma" / "files" / "render_maintenance.py",
)
assert _spec and _spec.loader
render_maintenance = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(render_maintenance)


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


def kuma_minutes(**overrides) -> set[int]:
    """The minutes of the week Kuma's maintenance window covers, from its rendered ConfigMap."""
    configmap = yaml_fast.safe_load(
        render_role_template(
            "uptime-kuma", "maintenance-sync-configmap.yaml.j2", overrides
        )
    )
    cron, duration = render_maintenance.window_cron(
        yaml_fast.safe_load(configmap["data"]["window.json"])
    )
    minute, hour, _, _, weekday = cron.split()
    opens = int(weekday) * MINUTES_PER_DAY + int(hour) * 60 + int(minute)
    return {(opens + offset) % MINUTES_PER_WEEK for offset in range(duration)}


def test_the_mute_equals_kumas_window_at_the_committed_schedule():
    """Sunday 07:25 to 08:15, the window both plans name."""
    # fact: ansible/roles/k8s/uptime-kuma/CLAUDE.md#The weekly reboot's maintenance window is reconciled over the API
    mute = mute_minutes()
    assert mute == kuma_minutes()
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
def test_moving_the_reboot_moves_the_mute_with_kumas_window(overrides):
    # fact: ansible/roles/k8s/observability/CLAUDE.md#Alert plane
    mute = mute_minutes(**overrides)
    assert mute == kuma_minutes(**overrides)
    assert mute != mute_minutes()


def test_a_mute_that_reads_a_different_schedule_is_flagged():
    """The rejecting half: a mute moved without the reboot no longer equals Kuma's window."""
    assert mute_minutes(weekly_reboot_hour=3) != kuma_minutes()
