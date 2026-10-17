"""Each expected host's HostUpdateStale limit outlasts its rendered updater schedule.

observability's `observability_expected_hosts` gives each host a limit in seconds. The host's
updater can go that long between two successes without a fault: the longest gap between two
timer fires, the timer's `RandomizedDelaySec`, and a run that takes the whole
`TimeoutStartSec` before it writes its timestamp. The schedule is the shared
`homelab-updater.timer` with the host package's drop-in over it, both as the host renderer
emits them, so a drop-in that outgrows a limit fails here before the rule pages a healthy host.

Run: uv run pytest scripts/renderer/tests/test_renderer_host_update_limits.py
"""

import re
from pathlib import PurePosixPath

import pytest


from lib import yaml_fast
from lib.repo_paths import REPO
from lib.systemd_timer import DAY_S, calendar_fires, span_s, timer_settings
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
# Triggers that fire once after boot. They add no gap once the host is up.
_BOOT_ONLY = frozenset({"OnBootSec", "OnStartupSec"})


def longest_gap_s(unit: str, dropins: list[str]) -> int:
    """The longest a timer can go between two fires once the host is up, in seconds.

    Raises:
        ValueError: A trigger this test cannot bound, or no trigger that repeats.
    """
    triggers, delay = timer_settings(unit, dropins)
    bounds = []
    for key, values in triggers.items():
        if key == "OnCalendar":
            fires = sorted({t for v in values for t in calendar_fires(v)})
            bounds.append(
                max(
                    b - a
                    for a, b in zip(fires, [*fires[1:], fires[0] + DAY_S], strict=True)
                )
            )
        elif key == "OnUnitActiveSec":
            bounds.append(min(span_s(v) for v in values))
        elif key not in _BOOT_ONLY:
            raise ValueError(f"{key}= is not a trigger this test bounds")
    if not bounds:
        raise ValueError("the timer has no trigger that repeats")
    # Every trigger fires at least once within its own bound, so the nearest one wins.
    return min(bounds) + delay


def timeout_start_s(unit: str) -> int:
    """The unit's `TimeoutStartSec=` in seconds."""
    (value,) = re.findall(r"^TimeoutStartSec=(.+)$", unit, re.MULTILINE)
    return span_s(value)


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
