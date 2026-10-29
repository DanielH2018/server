"""The `updater_lag_grace_s` filter in filter_plugins/updater_lag_grace.py.

monitor-bridge's env ConfigMap renders `BUILDER_MAX_LAG_S` through it, so the builder lag arm
outlasts the rollout canary's longest gap between updater fires (#6530). These tests read the
units from the tree, as the render does. `test_check_host_packages.py` checks the rendered
value reaching the verdict.

Run: uv run pytest ansible/tests/deploy/test_updater_lag_grace_filter.py
"""

import pytest
from ansible.errors import AnsibleFilterError

import updater_lag_grace as ulg
from lib.repo_paths import REPO

UNITS = REPO / "hosts/packages/homelab-updater/units"
TIMER = (UNITS / "homelab-updater.timer").read_text()
SERVICE = (UNITS / "homelab-updater.service").read_text()
PI_DROPIN = (REPO / "hosts/daniel-pi/units/homelab-updater.timer.conf").read_text()
FLOOR_S = 7200


def grace(dropin: str, floor=FLOOR_S) -> int:
    return ulg.FilterModule().filters()["updater_lag_grace_s"](
        TIMER, SERVICE, dropin, floor
    )


def test_the_pi_schedule_raises_the_grace_past_the_floor():
    # 22:50 to 01:05 is 8100 s, plus 60 s of randomized delay, the 3000 s run timeout and the
    # 600 s slack. A fixed 7200 s grace paged on that wait.
    assert grace(PI_DROPIN) == 8100 + 60 + 3000 + ulg.SLACK_S == 11760


def test_the_shared_five_minute_schedule_keeps_the_floor():
    # 300 s + 60 s + 3000 s + 600 s is under 7200 s, so the floor stands.
    assert grace("") == FLOOR_S


def test_a_floor_above_the_schedule_s_need_wins():
    assert grace(PI_DROPIN, floor="20000") == 20000


@pytest.mark.parametrize(
    ("dropin", "floor", "message"),
    [
        (
            "[Timer]\nOnCalendar=\nOnUnitInactiveSec=5min\n",
            FLOOR_S,
            "OnUnitInactiveSec",
        ),
        (PI_DROPIN, "two hours", "whole number of seconds"),
    ],
)
def test_a_schedule_or_floor_the_filter_cannot_read_is_flagged(dropin, floor, message):
    with pytest.raises(AnsibleFilterError, match=message):
        grace(dropin, floor)
