"""Tests for scripts/docs/catalog_lib/package_timers.py.

Run: uv run pytest scripts/docs/tests/test_package_timers.py
"""

from types import SimpleNamespace

import pytest

from docs.catalog_lib import package_timers as p

UNITS = "/usr/lib/systemd/system"


def _package(directory: str, files: dict[str, str], links: tuple[str, ...] = ()):
    return SimpleNamespace(
        directory=directory,
        files={dst: (text.encode(), 0o644) for dst, text in files.items()},
        enable_links=lambda: list(links),
    )


def _timers(own, shared=(), declared=None) -> dict[str, p.PackageTimer]:
    found = p.host_timers("box", own, list(shared), declared or {})
    return {timer.unit: timer for timer in found}


def test_a_host_drop_in_replaces_a_shared_timers_trigger():
    shared = _package(
        "packages/updater",
        {f"{UNITS}/u.timer": "[Timer]\nOnUnitActiveSec=5min\n"},
    )
    own = _package(
        "hosts/box",
        {f"{UNITS}/u.timer.d/at.conf": "[Timer]\nOnUnitActiveSec=\nOnCalendar=03:05\n"},
        links=("u.timer",),
    )
    timer = _timers(own, [shared])["u.timer"]
    assert timer.triggers == {"OnCalendar": ["03:05"]}
    assert timer.source == "hosts/packages/updater/package.yml"


def test_a_shared_timer_the_host_does_not_link_is_absent():
    shared = _package("packages/updater", {f"{UNITS}/u.timer": "[Timer]\n"})
    assert _timers(_package("hosts/box", {}), [shared]) == {}


def test_a_hosts_own_timer_without_a_link_reads_not_enabled():
    own = _package("hosts/box", {f"{UNITS}/x.timer": "[Timer]\nOnCalendar=weekly\n"})
    assert _timers(own)["x.timer"].enabled is False


def test_a_component_timer_names_its_source_and_role_and_service():
    own = _package(
        "hosts/box",
        {
            f"{UNITS}/x.timer": "[Timer]\nOnCalendar=weekly\n",
            f"{UNITS}/x.service": "[Service]\nExecStart=/bin/x\n",
        },
        links=("x.timer",),
    )
    timer = _timers(own, declared={"x.timer": ("hosts/components/c/x.timer", "r")})[
        "x.timer"
    ]
    assert timer.source == "hosts/components/c/x.timer"
    assert timer.role == "r"
    assert "ExecStart=/bin/x" in timer.service


@pytest.mark.parametrize(
    ("triggers", "minutes"),
    [
        ({"OnBootSec": ["2min"], "OnUnitActiveSec": ["2min"]}, 2.0),
        ({"OnUnitInactiveSec": ["6h"]}, 360.0),
        ({"OnCalendar": ["*-*-* 06,18:17:00"]}, 720.0),
        ({"OnCalendar": ["Sun *-*-* 09:00:00"]}, 10080.0),
        ({"OnCalendar": ["Sun *-*-* 07:30:00 UTC"]}, 10080.0),
        ({"OnCalendar": ["weekly"]}, 10080.0),
        ({"OnCalendar": ["*:0/5"]}, 5.0),
        ({"OnCalendar": ["Mon..Fri *-*-02..31 11:32:00"]}, None),
        ({"OnBootSec": ["5min"]}, None),
    ],
)
def test_cadence_minutes(triggers, minutes):
    assert p.cadence_minutes(triggers) == minutes


def test_the_live_packages_ship_the_named_timers_to_the_hosts_that_run_them():
    """Non-vacuity: an empty read, or a package that lost its timer, names the member here."""
    hosts: dict[str, set[str]] = {}
    for timer in p.package_timers():
        hosts.setdefault(timer.unit, set()).add(timer.host)
    assert hosts["homelab-docs-refresh.timer"] == {"daniel-box"}
    assert hosts["homelab-secret-rotation-audit.timer"] == {"daniel-box"}
    assert hosts["homelab-updater.timer"] == {
        "daniel-box",
        "daniel-pi",
        "daniel-server",
    }
    assert hosts["homelab-worktree-sweep.timer"] == {"daniel-box", "daniel-server"}
