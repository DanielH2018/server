"""The deploy-pipeline exporter reports the weekly reboot's no-promotion window (#5458).

`promote.py decide` defers every promotion inside the window `promote_lib/reboot_window.py`
derives from the `weekly_reboot_*` variables, so a green commit merged early on Sunday waits
past PromotionStalled's 2 hours with no hold. The exporter holds no target's verify timeout, so
it reports the widest window any target has, `chained_timeout.VERIFY_CAP_S` (5.5 hours) before
the reboot, the one `land-wait` reads. At the committed schedule (07:30 cron, a 5-minute
shutdown delay, 40 minutes of recovery) that is Sunday 02:05 to 08:15 UTC, end exclusive.

Run: uv run pytest ansible/roles/k8s/monitor-bridge/tests/test_deploy_pipeline_reboot_window.py
"""

from dataclasses import replace
from datetime import datetime, timezone
from datetime import timedelta

import pytest

import deploy_pipeline
from _bridge_env import UNDEFINED
from _deploy_pipeline_fakes import github
from _k8s_render import render_pod_env
from bridge.config import load_config
from deploy_tools import chained_timeout
from deploy_tools.promote_lib import reboot_window

GAUGE = "homelab_deploy_reboot_window_active"


def at(day: int, hour: int, minute: int = 0) -> float:
    """Seconds since the epoch at `hour:minute` UTC on 2026-10-`day`; the 18th is a Sunday."""
    return datetime(2026, 10, day, hour, minute, tzinfo=timezone.utc).timestamp()


@pytest.fixture
def piped(cfg):
    """The rendered config with a token set, so the family reads."""
    return replace(cfg, DEPLOY_PIPELINE_GITHUB_TOKEN="read-only-token")


def window_gauge(cfg, now: float) -> float | None:
    return deploy_pipeline.collect(cfg, github(), now)[0].get(GAUGE)


def test_the_gauge_reads_1_after_a_0300_commit_passes_two_hours(piped):
    """The issue's example: green at 03:00 Sunday, so PromotionStalled's 7200s ends at 05:00."""
    assert window_gauge(piped, at(18, 5, 5)) == 1


def test_the_gauge_reads_1_after_the_reboot_inside_the_mute(piped):
    assert window_gauge(piped, at(18, 8, 0)) == 1


def test_the_gauge_reads_0_at_the_window_end_when_the_scheduled_run_promotes(piped):
    assert window_gauge(piped, at(18, 8, 15)) == 0


def test_the_gauge_reads_0_at_the_same_hour_on_a_weekday(piped):
    assert window_gauge(piped, at(14, 8, 0)) == 0


def test_the_gauge_follows_the_weekly_reboot_variables(piped):
    """A reboot moved to 13:30 moves the window to 08:05-14:15, with no edit to the exporter."""
    env = {
        k: "" if str(v) == UNDEFINED else str(v)
        for k, v in render_pod_env("monitor-bridge", {"weekly_reboot_hour": 13}).items()
    }
    moved = replace(load_config(env), DEPLOY_PIPELINE_GITHUB_TOKEN="read-only-token")
    # The committed schedule's window holds 07:00 and not 13:00; the moved one the reverse.
    assert (window_gauge(piped, at(18, 7)), window_gauge(piped, at(18, 13))) == (1, 0)
    assert (window_gauge(moved, at(18, 7)), window_gauge(moved, at(18, 13))) == (0, 1)


def test_the_endpoint_serves_the_window_gauge(piped):
    found, _ = deploy_pipeline.collect(piped, github(), at(18, 8, 0))
    series = [
        line.split()[0]
        for line in deploy_pipeline.render(found).decode().splitlines()
        if not line.startswith("#")
    ]
    assert GAUGE in series


def test_the_cap_matches_chained_timeouts():
    """The pod cannot import chained_timeout.py, so the restated cap is held to its figure."""
    assert deploy_pipeline.VERIFY_CAP_S == chained_timeout.VERIFY_CAP_S


@pytest.mark.parametrize(
    "hour, minute, weekday",
    [(7, 30, 0), (13, 30, 0), (1, 0, 0), (23, 50, 6)],
    ids=["committed", "midday", "window-opens-saturday", "window-crosses-into-sunday"],
)
def test_the_gauge_agrees_with_promotes_window_across_a_week(
    piped, hour, minute, weekday
):
    """Every 5 minutes of a week, the gauge reads 1 exactly where `decide` defers the widest T."""
    cfg = replace(
        piped,
        WEEKLY_REBOOT_HOUR=hour,
        WEEKLY_REBOOT_MINUTE=minute,
        WEEKLY_REBOOT_WEEKDAY=weekday,
    )
    schedule = reboot_window.Schedule(
        minute=minute,
        hour=hour,
        weekday=weekday,
        delay_min=cfg.WEEKLY_REBOOT_SHUTDOWN_DELAY_MIN,
        lead_min=0,
        recovery_min=cfg.WEEKLY_REBOOT_MUTE_RECOVERY_MIN,
    )
    start = datetime(2026, 10, 12, tzinfo=timezone.utc)
    instants = [start + timedelta(minutes=5 * k) for k in range(7 * 24 * 12)]
    found = [deploy_pipeline.in_reboot_window(cfg, t.timestamp()) for t in instants]
    expected = [
        reboot_window.window(schedule, chained_timeout.VERIFY_CAP_S, t) is not None
        for t in instants
    ]
    assert found == expected
    assert any(found)
