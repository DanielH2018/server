"""The maintenance sync writes a window only when the live one differs, and writes it whole.

Every case pairs an input the script must pass over with one it must act on: a check that is
only ever observed passing is a check with no evidence it can fail.
"""

import json
import sys as _sys
from pathlib import Path as _Path

import pytest

ROLE = _Path(__file__).resolve().parents[1]
_sys.path.insert(0, str(ROLE / "files"))

from render_maintenance import main, window_cron  # noqa: E402

WINDOW = {
    "title": "Weekly system restart",
    "description": "derived from group_vars",
    "reboot_minute": 30,
    "reboot_hour": 7,
    "reboot_weekday": 0,
    "shutdown_delay_minutes": 5,
    "lead_minutes": 5,
    "recovery_allowance_minutes": 40,
    "timezone": "America/Chicago",
}

MONITORS = {
    "7": {"id": 7, "name": "k3s Grafana"},
    "8": {"id": 8, "name": "Daniel Pi Host"},
}

PAGE = {"id": 3, "slug": "containers", "title": "Homelab"}


def write(tmp_path, name, payload):
    path = tmp_path / name
    path.write_text(json.dumps(payload))
    return path


def plan(tmp_path, live, monitors=..., page=...):
    """Run the plan phase; return (desired-or-None, mode-or-None)."""
    out = tmp_path / "desired.json"
    mode = tmp_path / "mode"
    assert (
        main(
            [
                "--phase=plan",
                f"--window={write(tmp_path, 'window.json', WINDOW)}",
                f"--monitors={write(tmp_path, 'monitors.json', MONITORS if monitors is ... else monitors)}",
                f"--page={write(tmp_path, 'page.json', PAGE if page is ... else page)}",
                f"--live={write(tmp_path, 'live.json', live)}",
                f"--out={out}",
                f"--mode-out={mode}",
            ]
        )
        == 0
    )
    if not out.exists():
        assert not mode.exists()
        return None, None
    return json.loads(out.read_text()), mode.read_text()


def live_window(**overrides):
    """What `kuma maintenance get <id>` answers for a window already declared as derived."""
    cron, duration = window_cron(WINDOW)
    live = {
        "id": 4,
        "strategy": "cron",
        "title": WINDOW["title"],
        "description": WINDOW["description"],
        "active": True,
        "status": "scheduled",
        "cron": cron,
        "durationMinutes": float(duration),
        "dateRange": [None],
        "timeRange": None,
        "timezone": WINDOW["timezone"],
        "timezoneOption": WINDOW["timezone"],
        "timezoneOffset": "-05:00",
        "monitors": [{"id": 7, "pathName": "k3s Grafana"}, {"id": 8, "pathName": "x"}],
        "statusPages": [{"id": 3, "name": "Homelab"}],
    }
    live.update(overrides)
    return live


def test_the_window_is_derived_from_the_reboot_cron():
    assert window_cron(WINDOW) == ("25 7 * * 0", 50)


def test_a_lead_across_midnight_moves_the_window_to_the_previous_day():
    """07:30 leaves the arithmetic on one day; 00:02 is the case that must not render `-3 0`."""
    assert window_cron({**WINDOW, "reboot_hour": 0, "reboot_minute": 2}) == (
        "57 23 * * 6",
        50,
    )


def test_a_kuma_with_no_window_yet_gets_one_carrying_every_monitor(tmp_path):
    desired, mode = plan(tmp_path, None)
    assert mode == "add"
    assert "id" not in desired
    assert desired["cron"] == "25 7 * * 0"
    assert desired["durationMinutes"] == 50
    assert [monitor["id"] for monitor in desired["monitors"]] == [7, 8]
    assert desired["statusPages"] == [{"id": 3}]
    # The keys kuma-client has no serde default for, and Kuma's jsonToBean indexes.
    assert desired["dateRange"] == [None]
    assert desired["timezoneOption"] == "America/Chicago"


def test_a_window_that_already_says_it_is_left_alone(tmp_path):
    assert plan(tmp_path, live_window()) == (None, None)


def test_a_monitor_kuma_gained_since_the_last_run_is_added_to_the_live_window(tmp_path):
    """Membership is the field that decays: `maintenance list` never carries it."""
    desired, mode = plan(
        tmp_path,
        live_window(monitors=[{"id": 7, "pathName": "k3s Grafana"}]),
    )
    assert mode == "edit"
    assert desired["id"] == 4
    assert [monitor["id"] for monitor in desired["monitors"]] == [7, 8]


def test_a_moved_reboot_moves_the_live_window(tmp_path):
    desired, mode = plan(tmp_path, live_window(cron="25 6 * * 0"))
    assert (mode, desired["cron"]) == ("edit", "25 7 * * 0")


def test_a_paused_window_is_reactivated(tmp_path):
    """The way back from a hand-paused window, which suppresses nothing while it is off."""
    desired, mode = plan(tmp_path, live_window(active=False))
    assert (mode, desired["active"]) == ("edit", True)


def test_a_single_element_array_is_read_as_the_window_it_holds(tmp_path):
    """kuma-cli prints one result as an object and several as an array; misreading the array
    as `no window` would add a second window beside the one Kuma already has."""
    assert plan(tmp_path, [live_window()]) == (None, None)


def test_a_detail_shape_this_cannot_edit_stops_the_reconcile(tmp_path):
    with pytest.raises(SystemExit) as excinfo:
        plan(tmp_path, "not a maintenance")
    assert "unreadable maintenance detail" in str(excinfo.value)


def test_an_empty_monitor_list_fails_rather_than_emptying_the_window(tmp_path):
    with pytest.raises(SystemExit) as excinfo:
        plan(tmp_path, live_window(), monitors={})
    assert "no monitors" in str(excinfo.value)


def test_two_windows_with_one_title_stop_the_reconcile(tmp_path):
    """Picking one would leave the other suppressing on its own schedule forever."""
    out = tmp_path / "live-id.txt"
    duplicates = {
        "4": {"id": 4, "title": WINDOW["title"]},
        "9": {"id": 9, "title": WINDOW["title"]},
    }
    with pytest.raises(SystemExit) as excinfo:
        main(
            [
                "--phase=select",
                f"--window={write(tmp_path, 'window.json', WINDOW)}",
                f"--maintenances={write(tmp_path, 'maintenances.json', duplicates)}",
                f"--out={out}",
            ]
        )
    assert "[4, 9]" in str(excinfo.value)


def test_the_select_phase_reports_the_id_the_detail_stage_reads(tmp_path):
    out = tmp_path / "live-id.txt"
    listed = {
        "4": {"id": 4, "title": WINDOW["title"]},
        "5": {"id": 5, "title": "something else"},
    }
    args = [
        "--phase=select",
        f"--window={write(tmp_path, 'window.json', WINDOW)}",
        f"--maintenances={write(tmp_path, 'maintenances.json', listed)}",
        f"--out={out}",
    ]
    assert main(args) == 0
    assert out.read_text() == "4"

    write(tmp_path, "maintenances.json", {"5": {"id": 5, "title": "something else"}})
    assert main(args) == 0
    assert out.read_text() == ""
