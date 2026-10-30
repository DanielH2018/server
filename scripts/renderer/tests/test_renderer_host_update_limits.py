"""Each expected host's HostUpdateStale limit outlasts its rendered updater schedule.

observability's `observability_expected_hosts` gives each host a limit in seconds. The host's
updater can go that long between two successes without a fault: the longest gap between two
timer fires, the timer's `RandomizedDelaySec`, and a run that takes the whole
`TimeoutStartSec` before it writes its timestamp. The schedule is the shared
`homelab-updater.timer` with the host package's drop-in over it, both as the host renderer
emits them, so a drop-in that outgrows a limit fails here before the rule pages a healthy host.

DockerEnginePinUnapplied's window is sized from the same schedule and the Pi's engine timer, so
a moved engine window or a longer engine run fails here before that rule pages a pin that is
still waiting (#6607).

Run: uv run pytest scripts/renderer/tests/test_renderer_host_update_limits.py
"""

import re
from pathlib import PurePosixPath

import pytest


from lib import yaml_fast
from lib.repo_paths import REPO
from lib.systemd_timer import (
    DAY_S,
    calendar_fires,
    longest_gap_s,
    span_s,
    timeout_start_s,
    timer_settings,
)
from renderer.renderer_lib import host as host_mode

DEFAULTS = (
    REPO / "ansible" / "roles" / "k8s" / "observability" / "defaults" / "main.yml"
)
UNITS = PurePosixPath("/usr/lib/systemd/system")
TIMER = UNITS / "homelab-updater.timer"
SERVICE = UNITS / "homelab-updater.service"
DROPIN = UNITS / "homelab-updater.timer.d" / "50-homelab.conf"
# The Pi's drop-in, as H3 specifies it and the container cases install it. H3.6 ships it in
# hosts/daniel-pi/; until then this copy is the Pi's schedule.
PI_DROPIN = REPO / "hosts/lib/tests/container/fixture/units/updater-timer.conf"


def limit_needed_s(timer: str, dropins: list[str], service: str) -> int:
    """The shortest limit a host on this schedule can carry without paging between runs."""
    return longest_gap_s(timer, dropins) + timeout_start_s(service)


def limit_problems(
    expected: dict[str, dict], render_host=host_mode.render_host
) -> list[str]:
    """Why a host's limit is shorter than its rendered schedule allows, or nothing.

    Args:
        expected: An `observability_expected_hosts` map.
        render_host: Renders a host's package, as `host_mode.render_host` does.
    """
    updater = host_mode.render_package("homelab-updater")
    timer = updater.files[str(TIMER)][0].decode()
    service = updater.files[str(SERVICE)][0].decode()
    problems = []
    for host, entry in sorted(expected.items()):
        package = render_host(host)
        dropins = (
            [package.files[str(DROPIN)][0].decode()]
            if str(DROPIN) in package.files
            else []
        )
        needed = limit_needed_s(timer, dropins, service)
        if entry["limit_seconds"] < needed:
            problems.append(
                f"{host}: limit_seconds is {entry['limit_seconds']}, and its rendered "
                f"updater schedule needs at least {needed}"
            )
    return problems


@pytest.fixture
def pi_host(hosts, tmp_path):
    """A `hosts/` tree whose daniel-pi package ships the Pi's updater drop-in."""
    root = hosts({"files": [{"src": "units/updater-timer.conf", "dst": str(DROPIN)}]})
    (root / "daniel-pi" / "units").mkdir()
    (root / "daniel-pi" / "units" / "updater-timer.conf").write_text(
        PI_DROPIN.read_text()
    )
    return lambda host: host_mode.render_host(host, root)


def test_every_expected_host_limit_outlasts_its_schedule_is_clean():
    expected = yaml_fast.safe_load(DEFAULTS.read_text())["observability_expected_hosts"]
    assert limit_problems(expected) == []


def test_the_shared_schedule_needs_its_interval_delay_and_timeout():
    # Named member: the shared timer renders, and a host with no drop-in needs 5min, 60s and
    # 50min.
    assert limit_problems(
        {"daniel-box": {"limit_seconds": 3359}}, lambda h: host_mode.Package(h, h)
    ) == [
        "daniel-box: limit_seconds is 3359, and its rendered updater schedule needs at least 3360"
    ]


def test_a_limit_the_pi_schedule_outlasts_is_flagged(pi_host):
    # 22:50 to 01:05 is 8100s, plus 60s of randomized delay and the 3000s timeout.
    [problem] = limit_problems({"daniel-pi": {"limit_seconds": 11159}}, pi_host)
    assert problem.endswith("needs at least 11160")


def test_a_limit_that_outlasts_the_pi_schedule_is_clean(pi_host):
    assert limit_problems({"daniel-pi": {"limit_seconds": 11400}}, pi_host) == []


@pytest.mark.parametrize(
    ("expression", "first", "count"),
    [
        ("*-*-* 01..03,06..22:05/15:00", 3900, 80),
        ("00:30", 1800, 1),
        ("*-*-* *:00/30", 0, 48),
    ],
)
def test_a_daily_calendar_expands_to_its_fires(expression, first, count):
    fires = calendar_fires(expression)
    assert (fires[0], len(fires)) == (first, count)


@pytest.mark.parametrize("expression", ["Mon *-*-* 00:00", "daily", "*-*-01 00:00"])
def test_a_calendar_this_test_cannot_expand_is_flagged(expression):
    with pytest.raises(ValueError, match="not a daily schedule"):
        calendar_fires(expression)


def test_an_empty_assignment_resets_the_inherited_triggers():
    unit = "[Timer]\nOnBootSec=5min\nOnUnitActiveSec=5min\nRandomizedDelaySec=60\n"
    dropin = "[Timer]\nOnCalendar=\nOnCalendar=*-*-* 00:30:00\n"
    assert longest_gap_s(unit, [dropin]) == DAY_S + 60


def test_a_trigger_this_test_cannot_bound_is_flagged():
    with pytest.raises(ValueError, match="OnUnitInactiveSec"):
        longest_gap_s("[Timer]\nOnUnitInactiveSec=5min\n", [])


RULES = (
    REPO
    / "ansible"
    / "roles"
    / "k8s"
    / "observability"
    / "files"
    / "rules"
    / "host-checks.yaml"
)
ENGINE_UNITS = REPO / "hosts" / "components" / "pi-docker" / "units"
UPDATER_UNITS = REPO / "hosts" / "packages" / "homelab-updater" / "units"
PI_UPDATER_DROPIN = (
    REPO / "hosts" / "daniel-pi" / "units" / "homelab-updater.timer.conf"
)


def _daily_fires(timer: str, dropins: list[str]) -> list[int]:
    triggers, _ = timer_settings(timer, dropins)
    return sorted({t for v in triggers["OnCalendar"] for t in calendar_fires(v)})


def _next_fire(fires: list[int], after: int) -> int:
    """The first fire strictly after `after`, a time that may run past one day."""
    day, offset = divmod(after, DAY_S)
    later = [f for f in fires if f > offset]
    return day * DAY_S + (later[0] if later else fires[0] + DAY_S)


def engine_window_needed_s(
    engine_timer: str,
    engine_service: str,
    updater_timer: str,
    updater_dropins: list[str],
    updater_service: str,
) -> int:
    """The longest homelab_docker_engine_pending can legitimately read 1, in seconds.

    The updater first writes 1 at its first fire after an engine window, and the pin waits for
    the next window. The engine run then takes up to its `TimeoutStartSec`, and the updater's
    next fire, its randomized delay and its own timeout rewrite the 1 to 0.
    """
    engine = _daily_fires(engine_timer, [])
    updater = _daily_fires(updater_timer, updater_dropins)
    _, delay = timer_settings(updater_timer, updater_dropins)
    engine_timeout = timeout_start_s(engine_service)
    needed = 0
    for window in engine:
        written = _next_fire(updater, window)
        applied = _next_fire(engine, written)
        rewritten = _next_fire(updater, applied + engine_timeout)
        needed = max(
            needed, rewritten + delay + timeout_start_s(updater_service) - written
        )
    return needed


def rule_windows_s(rules: str) -> list[int]:
    """DockerEnginePinUnapplied's `min_over_time` range and its guard's offset, in seconds."""
    [rule] = [
        r
        for g in yaml_fast.safe_load(rules)["groups"]
        for r in g["rules"]
        if r.get("alert") == "DockerEnginePinUnapplied"
    ]
    return [
        span_s(v)
        for pair in re.findall(
            r"min_over_time\(.*?\[(\w+)\]|offset (\w+)", rule["expr"]
        )
        for v in pair
        if v
    ]


def test_the_engine_pending_window_outlasts_the_pi_schedule_is_clean():
    needed = engine_window_needed_s(
        (ENGINE_UNITS / "homelab-docker-engine.timer").read_text(),
        (ENGINE_UNITS / "homelab-docker-engine.service").read_text(),
        (UPDATER_UNITS / "homelab-updater.timer").read_text(),
        [PI_UPDATER_DROPIN.read_text()],
        (UPDATER_UNITS / "homelab-updater.service").read_text(),
    )
    # Named member: 01:05 to the next 00:30 is 84300s, plus the 5400s engine run, the 300s to
    # the 02:05 updater fire, its 60s delay and its 3000s run.
    assert needed == 93060
    windows = rule_windows_s(RULES.read_text())
    # The `[26h]` range and the `offset 26h` presence guard.
    assert len(windows) == 2
    assert min(windows) >= needed


def test_an_engine_run_that_outgrows_the_rule_window_is_flagged():
    service = (
        (ENGINE_UNITS / "homelab-docker-engine.service")
        .read_text()
        .replace("TimeoutStartSec=90min", "TimeoutStartSec=3h")
    )
    needed = engine_window_needed_s(
        (ENGINE_UNITS / "homelab-docker-engine.timer").read_text(),
        service,
        (UPDATER_UNITS / "homelab-updater.timer").read_text(),
        [PI_UPDATER_DROPIN.read_text()],
        (UPDATER_UNITS / "homelab-updater.service").read_text(),
    )
    assert needed > min(rule_windows_s(RULES.read_text()))
