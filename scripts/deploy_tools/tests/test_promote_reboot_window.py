"""The weekly reboot's no-promotion window: its derivation, `decide`'s `deferred`, the cron, land-wait.

The plan is docs/target-architecture/deploy/d5-bulk-move.md, *The stateful hold, its post,
and the reset input*, *No promotion runs into the weekly reboot*. `World` commits the
committed schedule (`REBOOT_YAML`): the cron fires Sunday 07:30 UTC, the hosts go down at
07:35, and M2's mute ends at 08:15.

Run: uv run pytest scripts/deploy_tools/tests/test_promote_reboot_window.py
"""

import dataclasses
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from _land_wait_fakes import P, Sim, at
from _promote_fakes import NOW, REBOOT_YAML, World
from lib import yaml_fast
from lib.repo_paths import REPO

import promote
from chained_timeout import VERIFY_CAP_S
from deploy_tools.promote_lib import reboot_window
from deploy_tools.promote_lib import verify_promotion as vp
from deploy_tools.promote_lib import wait as promote_wait

WORKFLOW = REPO / ".github" / "workflows" / "promote.yml"
# A Sunday, and the instants around its reboot.
SUNDAY = datetime(2026, 10, 18, tzinfo=UTC)
END = SUNDAY.replace(hour=8, minute=15)


class Clock:
    """Monotonic seconds that advance only when `decide` sleeps."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def _schedule(text: str, tmp_path: Path) -> reboot_window.Schedule:
    path = tmp_path / reboot_window.SOURCE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return reboot_window.read_schedule(tmp_path)


def _cron(workflow: dict) -> str:
    # YAML 1.1 reads the bare key `on` as the boolean True.
    triggers = workflow.get("on", workflow.get(True))
    return triggers["schedule"][0]["cron"]


def _reads(world, now: datetime, clock: Clock) -> promote.Reads:
    reads = promote.live_reads(world.clients(), world.repo, "master", now)
    return dataclasses.replace(reads, sleep=clock.sleep, clock=clock)


def _settled_world(world) -> str:
    """`prod` and `last-good` on c1, so nothing is in flight, and the tip published."""
    _, c1, _, tip = world.commits
    world.set_prod(c1)
    world.set_last_good(c1)
    world.publish(tip)
    return tip


# -- the derivation ------------------------------------------------------------------------


def test_the_window_derives_from_the_reboot_variables_is_clean(tmp_path):
    """The red half is the moved hour: code reading a literal 7 still holds 05:10 inside."""
    committed = _schedule(REBOOT_YAML, tmp_path / "committed")
    two_and_a_half_hours = 150 * 60
    at_0510 = SUNDAY.replace(hour=5, minute=10)
    assert reboot_window.window(committed, two_and_a_half_hours, at_0510) == (
        SUNDAY.replace(hour=5, minute=5),
        END,
    )
    assert reboot_window.window(committed, two_and_a_half_hours, END) is None

    moved = _schedule(
        REBOOT_YAML.replace("weekly_reboot_hour: 7", "weekly_reboot_hour: 9"),
        tmp_path / "moved",
    )
    assert reboot_window.window(moved, two_and_a_half_hours, at_0510) is None
    assert reboot_window.window(moved, two_and_a_half_hours, at_0510.replace(hour=7))


def test_the_live_inventory_names_every_variable_the_window_reads():
    assert isinstance(reboot_window.read_schedule(REPO), reboot_window.Schedule)


@pytest.mark.parametrize(
    "edit",
    [
        ("weekly_reboot_hour: 7", "weekly_reboot_hour: '{{ reboot_hour }}'"),
        ("weekly_reboot_mute_recovery_minutes: 40\n", ""),
        ("weekly_reboot_timezone: UTC", "weekly_reboot_timezone: America/Chicago"),
        ("weekly_reboot_weekday: 0", "weekly_reboot_weekday: 7"),
    ],
    ids=["a Jinja string", "a missing key", "a zone other than UTC", "out of range"],
)
def test_an_unusable_schedule_is_a_read_error(tmp_path, edit):
    with pytest.raises(reboot_window.ReadError):
        _schedule(REBOOT_YAML.replace(*edit), tmp_path)


# -- the cron ------------------------------------------------------------------------------


def test_the_workflows_cron_is_the_derived_window_end_is_clean():
    workflow = yaml_fast.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert _cron(workflow) == reboot_window.end_cron(reboot_window.read_schedule(REPO))


def test_a_reboot_moved_without_the_cron_is_flagged(tmp_path):
    workflow = yaml_fast.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    moved = _schedule(
        REBOOT_YAML.replace("weekly_reboot_hour: 7", "weekly_reboot_hour: 9"), tmp_path
    )
    assert reboot_window.end_cron(moved) == "15 10 * * 0"
    assert _cron(workflow) != reboot_window.end_cron(moved)


def test_a_window_that_ends_past_midnight_moves_the_crons_day(tmp_path):
    late = _schedule(
        REBOOT_YAML.replace("weekly_reboot_hour: 7", "weekly_reboot_hour: 23"), tmp_path
    )
    assert reboot_window.end_cron(late) == "15 0 * * 1"


# -- decide --------------------------------------------------------------------------------


def test_a_run_inside_ts_window_ends_deferred_moves_nothing_and_never_sleeps(world):
    tip = _settled_world(world)
    clock = Clock()
    decision = promote.decide(_reads(world, END - timedelta(minutes=30), clock))
    assert decision.verdict == "deferred"
    assert decision.target == ""
    assert tip in decision.reason and "no-promotion window" in decision.reason
    assert clock.now == 0, "decide waited in its job"


def test_a_deferred_run_exits_0_and_hands_tag_no_target(world, monkeypatch, capsys):
    _settled_world(world)
    result = world.decide(monkeypatch, capsys, now=END - timedelta(minutes=1))
    assert result.exit_code == 0
    assert (result.outputs["verdict"], result.outputs["target"]) == ("deferred", "")
    assert world.run.written == []


def test_the_window_opens_ts_own_timeout_before_the_reboot(world):
    """The empty bundle's T verifies in minutes, so 06:00 is outside its window."""
    tip = _settled_world(world)
    early = SUNDAY.replace(hour=6)
    decision = promote.decide(_reads(world, early, Clock()))
    assert (decision.verdict, decision.target) == ("promote", tip)
    target = decision.bundles["target"]
    assert target is not None
    timeout = vp.expected_set(target, decision.bundles["prod"])
    reboot = SUNDAY.replace(hour=7, minute=35)
    assert early < reboot - timedelta(seconds=timeout.timeout_s)
    assert early < SUNDAY.replace(hour=7, minute=35) - timedelta(
        seconds=timeout.timeout_s
    )


def test_the_scheduled_run_at_the_windows_end_hands_verify_its_full_timeout(world):
    """The red half is a decide that waits in its job: its budget is the cap less the wait."""
    tip = _settled_world(world)
    clock = Clock()
    decision = promote.decide(_reads(world, END, clock))
    assert (decision.verdict, decision.target) == ("promote", tip)
    assert VERIFY_CAP_S - clock.now == VERIFY_CAP_S

    def waiting_decide(reads: promote.Reads) -> promote.Decision:
        found = reboot_window.window(reads.reboot(), VERIFY_CAP_S, reads.now())
        assert found is not None
        end = found[1]
        reads.sleep((end - reads.now()).total_seconds())
        return promote.decide(dataclasses.replace(reads, now=lambda: end))

    waiting = Clock()
    late = waiting_decide(_reads(world, END - timedelta(hours=2), waiting))
    assert late.verdict == "promote"
    assert VERIFY_CAP_S - waiting.now == VERIFY_CAP_S - 2 * 60 * 60


def test_a_run_outside_every_window_promotes(world):
    tip = _settled_world(world)
    decision = promote.decide(_reads(world, NOW, Clock()))
    assert (decision.verdict, decision.target) == ("promote", tip)


# -- land-wait -----------------------------------------------------------------------------


def test_a_merge_inside_the_window_gives_up_from_its_end_not_from_the_merge(capsys):
    """The red half is the same wait with no window, which gives up at the base timeout."""
    left = 3600
    # The live read counts down one poll at a time, then reads None once the window ends.
    countdown = [
        (left - n * promote_wait.POLL_S, END)
        for n in range(left // promote_wait.POLL_S)
    ]
    inside = Sim(polls=[at(P)], reboot_window=[*countdown, None])
    result = inside.wait()
    assert result.verdict == "gave-up"
    waited = inside.sleeps * promote_wait.POLL_S
    assert waited >= left + promote_wait.LAND_WAIT_TIMEOUT_S
    assert "Sun 08:15 UTC" in capsys.readouterr().out

    outside = Sim(polls=[at(P)])
    assert outside.wait().verdict == "gave-up"
    assert outside.sleeps * promote_wait.POLL_S == promote_wait.LAND_WAIT_TIMEOUT_S


def test_land_waits_live_window_is_the_widest_any_t_has():
    schedule = reboot_window.read_schedule(REPO)
    opened = SUNDAY.replace(hour=7, minute=35) - timedelta(seconds=VERIFY_CAP_S)
    assert reboot_window.remaining(schedule, VERIFY_CAP_S, opened) == (
        int((END - opened).total_seconds()),
        END,
    )
    assert (
        reboot_window.remaining(schedule, VERIFY_CAP_S, opened - timedelta(minutes=1))
        is None
    )
