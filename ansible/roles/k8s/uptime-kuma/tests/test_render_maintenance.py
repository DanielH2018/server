"""The maintenance sync writes a window only when the live one differs, and writes it whole.

Every case pairs an input the script must pass over with one it must act on: a check that is
only ever observed passing is a check with no evidence it can fail.
"""

import json
from pathlib import Path as _Path

import pytest

from render_maintenance import down_monitor_ids, main, window_cron

ROLE = _Path(__file__).resolve().parents[1]

WINDOW = {
    "title": "Weekly system restart",
    "description": "derived from group_vars",
    "reboot_minute": 30,
    "reboot_hour": 7,
    "reboot_weekday": 0,
    "shutdown_delay_minutes": 5,
    "lead_minutes": 5,
    "recovery_allowance_minutes": 40,
    "down_exclusion_minutes": 60,
    "timezone": "America/Chicago",
}

# The :20 sync run five minutes before the 07:25 opening, on a Sunday (2026-10-04).
BEFORE_OPENING = "2026-10-04T07:20:30+00:00"
# The run after the window closes, which puts a left-out monitor back.
AFTER_CLOSING = "2026-10-04T08:20:30+00:00"

MONITORS = {
    "7": {"id": 7, "name": "k3s Grafana"},
    "8": {"id": 8, "name": "Daniel Pi Host"},
}

PAGE = {"id": 3, "slug": "containers", "title": "Homelab"}


def write(tmp_path, name, payload):
    path = tmp_path / name
    path.write_text(json.dumps(payload))
    return path


def plan(tmp_path, live, monitors=..., page=..., down=None, now=BEFORE_OPENING):
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
                f"--down={write(tmp_path, 'down.json', down)}",
                f"--now={now}",
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


def test_a_monitor_already_down_is_left_out_of_the_window_about_to_open(tmp_path):
    """#3506: Kuma pages `MAINTENANCE -> DOWN`, so a DOWN monitor carried through the window
    pages a second time when it closes."""
    desired, mode = plan(tmp_path, live_window(), down=[8], now=BEFORE_OPENING)
    assert mode == "edit"
    assert [monitor["id"] for monitor in desired["monitors"]] == [7]


def test_the_run_after_the_window_puts_a_left_out_monitor_back(tmp_path):
    desired, mode = plan(
        tmp_path,
        live_window(monitors=[{"id": 7, "pathName": "k3s Grafana"}]),
        down=[8],
        now=AFTER_CLOSING,
    )
    assert mode == "edit"
    assert [monitor["id"] for monitor in desired["monitors"]] == [7, 8]


def test_a_down_monitor_stays_in_the_window_on_every_other_run(tmp_path):
    """Only the run before the opening edits for a DOWN monitor, so a flapping one does not
    rewrite ~85 monitor_maintenance rows every hour."""
    assert plan(tmp_path, live_window(), down=[8], now="2026-10-04T06:20:30+00:00") == (
        None,
        None,
    )


def test_an_unreadable_status_keeps_every_monitor_in_the_window(tmp_path):
    assert plan(tmp_path, live_window(), down=None, now=BEFORE_OPENING) == (None, None)


def test_every_monitor_down_keeps_every_monitor_in_the_window(tmp_path):
    """That is Kuma's own network failing; an empty window would page the reboot for all."""
    assert plan(tmp_path, live_window(), down=[7, 8], now=BEFORE_OPENING) == (
        None,
        None,
    )


def test_only_a_down_series_counts_as_down():
    metrics = "\n".join(
        [
            "# HELP monitor_status Monitor Status (1 = UP, 0= DOWN, 2= PENDING, 3= MAINTENANCE)",
            "# TYPE monitor_status gauge",
            'monitor_status{monitor_id="449",monitor_name="Arr Queue Warnings",'
            'monitor_type="push",monitor_url="https://",monitor_hostname="null",'
            'monitor_port="null"} 0',
            'monitor_status{monitor_id="7",monitor_name="k3s Grafana",monitor_type="http"} 1',
            'monitor_status{monitor_id="8",monitor_name="Pending",monitor_type="push"} 2',
            'monitor_status{monitor_id="9",monitor_name="In window",monitor_type="push"} 3',
            'monitor_response_time{monitor_id="10",monitor_name="x"} 0',
            'monitor_status{monitor_name="no id",monitor_type="push"} 0',
        ]
    )
    assert down_monitor_ids(metrics) == [449]


def status(tmp_path, url, key):
    out = tmp_path / "down.json"
    key_file = tmp_path / "metrics_api_key"
    key_file.write_text(key)
    assert (
        main(
            [
                "--phase=status",
                f"--metrics-url={url}",
                f"--api-key-file={key_file}",
                f"--out={out}",
            ]
        )
        == 0
    )
    return json.loads(out.read_text())


def test_an_unreachable_exporter_writes_null_rather_than_failing_the_job(tmp_path):
    """A failed job would also stop adding new monitors to the window."""
    assert status(tmp_path, "http://127.0.0.1:1/metrics", "uk1_key") is None


def test_no_api_key_writes_null(tmp_path):
    assert status(tmp_path, "http://127.0.0.1:1/metrics", "") is None
