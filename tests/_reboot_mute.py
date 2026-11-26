"""Alertmanager's `weekly-reboot` mute, as rendered and as the reboot schedule implies.

`mute_minutes` reads the mute Alertmanager is given. `window_minutes` is the sum the template
does, from the `weekly_reboot_*` values in estate/vars.yml alone. Both count minutes of the
week from Sunday 00:00, and both take the same template-variable overrides.
"""

from lib.estate import shared_vars
from lib import yaml_fast
from _k8s_render import render_role_template


GROUP_VARS = shared_vars()
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
