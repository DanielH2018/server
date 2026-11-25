"""Tests for `probe.py monitors --checks`, the per-check verdict view.

The series are in the shape Prometheus returns for `monitor_checks.CHECK_QUERY`; the streak
line is one monitor-bridge logged on 2026-11-15.
"""

from types import SimpleNamespace


import pytest
from diagnostics.probe_lib import core, monitor_checks
from lib.exit_codes import PROBE_FAILED, PROBE_OK, PROBE_UNREADABLE

NOW = 1794777200.0
STREAK = (
    "OK   ha_heartbeat - down streak 1/2 (deploy/restart grace): HA API unreachable: "
    "home-assistant.homelab.svc.cluster.local:8123: <urlopen error timed out>"
)


def _check(name, value=1, age=60, deadline=900, details=()):
    def s(metric, v, **extra):
        return {
            "metric": {"__name__": metric, "check": name, **extra},
            "value": [NOW, str(v)],
        }

    out = [
        s("homelab_check_success", value),
        s("homelab_check_last_run_timestamp_seconds", NOW - age),
        s("homelab_check_deadline_seconds", deadline),
    ]
    return out + [s("homelab_check_detail", 1, detail=d) for d in details]


def _report(
    series, held=None, gates=None, names=None, rules=None, only="", suppressed=None
):
    rows = monitor_checks.rows_of(series)
    return monitor_checks.report(
        rows,
        table_gates=gates or {},
        table_names=list(rows) if names is None else list(names),
        rules={n: [] for n in rows} if rules is None else rules,
        held={} if held is None else held,
        suppressed={} if suppressed is None else suppressed,
        now=NOW,
        only=only,
    )


def _line(text, name):
    return next(line for line in text.splitlines() if line.startswith(f"  {name} "))


def test_every_up_check_is_listed_with_its_last_run_age():
    text, code = _report(_check("n8n") + _check("r2_usage"))
    assert code == PROBE_OK
    assert text.splitlines()[0] == "2 checks: 2 up"
    assert "1 min ago" in _line(text, "n8n")


def test_a_down_check_names_its_failing_members_and_fails():
    text, code = _report(_check("prowlarr_indexers", 0, details=("nyaa", "1337x")))
    assert code == PROBE_FAILED
    assert _line(text, "prowlarr_indexers").endswith("down: 1337x, nyaa")


def test_a_row_behind_a_down_gate_is_skipped_not_down():
    series = _check("wan_reachable", 0) + _check("discord", 0)
    text, _ = _report(series, gates={"discord": "wan_reachable"})
    assert _line(text, "discord").endswith("skipped: gate wan_reachable down")
    assert "1 down, 1 skipped" in text.splitlines()[0]


def test_a_gate_with_no_row_of_its_own_does_not_explain_a_zero():
    text, _ = _report(_check("n8n", 0), gates={"n8n": "startup_grace"})
    assert _line(text, "n8n").endswith("down")


def test_a_run_older_than_its_deadline_is_stale_and_fails():
    text, code = _report(_check("scrutiny", age=901))
    assert code == PROBE_FAILED
    assert "stale: past 900s" in _line(text, "scrutiny")


def test_a_streak_line_from_the_rows_newest_cycle_is_its_note():
    rows = monitor_checks.rows_of(_check("ha_heartbeat", age=60))
    held = monitor_checks.streaks([(int((NOW - 62) * 1e9), STREAK)], rows)
    assert held == {"ha_heartbeat": "down streak 1/2"}
    text, code = _report(_check("ha_heartbeat", age=60), held=held)
    assert code == PROBE_OK
    assert _line(text, "ha_heartbeat").endswith("down streak 1/2")


def test_a_streak_line_from_an_older_cycle_is_dropped():
    rows = monitor_checks.rows_of(_check("ha_heartbeat", age=60))
    assert monitor_checks.streaks([(int((NOW - 360) * 1e9), STREAK)], rows) == {}


def test_an_unread_loki_says_so_rather_than_reading_as_no_streak():
    text, code = _report(_check("n8n"), held="loki: timed out")
    assert code == PROBE_OK
    assert "streak notes unread: loki: timed out" in text


def test_a_check_with_no_rule_is_flagged_as_never_paging():
    text, _ = _report(_check("n8n"), rules={})
    assert "no rule in bridge-exporter.yaml" in _line(text, "n8n")


def test_a_down_check_names_the_rule_that_selects_it():
    text, _ = _report(
        _check("b2_object_lock", 0), rules={"b2_object_lock": ["B2ObjectLockOff"]}
    )
    assert _line(text, "b2_object_lock").endswith("down; rule B2ObjectLockOff")


def test_a_live_row_this_checkout_lacks_is_not_called_ruleless():
    text, _ = _report(_check("b2_object_lock", 0), names=[], rules={})
    line = _line(text, "b2_object_lock")
    assert "never pages" not in line
    assert line.endswith("not in this checkout's check table, pull to read its rule")


def test_a_table_row_with_no_series_reads_not_exported():
    text, code = _report(_check("n8n"), names=["n8n", "bazarr"])
    assert code == PROBE_OK
    assert _line(text, "bazarr").endswith("not exported")


def test_a_name_narrows_the_rows_and_an_unknown_one_is_refused():
    text, _ = _report(_check("b2_storage") + _check("n8n"), only="b2")
    assert "n8n" not in text and "b2_storage" in text
    with pytest.raises(core.UsageError):
        _report(_check("n8n"), only="nope")


def test_the_real_rule_file_and_table_cover_a_known_check():
    rules = monitor_checks.rules_by_check(monitor_checks.RULES.read_text())
    assert rules["b2_object_lock"] == ["B2ObjectLockOff"]
    gates = monitor_checks.gates(monitor_checks.CHECK_TABLE.read_text())
    assert gates["discord"] == "wan_reachable"


def test_no_check_series_is_unreadable():
    def empty(*_a, **_k):
        return {"status": "success", "data": {"result": []}}

    with pytest.raises(core.Unreadable) as exc:
        monitor_checks.print_checks(
            SimpleNamespace(dry_run=False, checks=""), fetch=empty
        )
    assert exc.value.code == PROBE_UNREADABLE


def test_a_row_behind_a_dead_exporter_is_skipped_not_down():
    up = [{"metric": {"job": "node", "instance": "daniel-box"}, "value": [NOW, "0"]}]
    suppressed = monitor_checks.suppressed_checks(up, {"node": {"host_packages"}})
    text, code = _report(_check("host_packages", 0), suppressed=suppressed)
    assert code == PROBE_OK
    assert _line(text, "host_packages").endswith("skipped: exporter node down")


def test_an_unread_up_leaves_the_row_down_and_says_why():
    text, code = _report(_check("host_packages", 0), suppressed="curl: timed out")
    assert code == PROBE_FAILED
    assert (
        "exporter state unread, so no row reads skipped by one: curl: timed out" in text
    )
    assert _line(text, "host_packages").endswith("down")


def test_the_real_exporter_map_names_host_packages_under_node():
    source = monitor_checks.CHECK_MODULE.read_text()
    assert "host_packages" in monitor_checks.exporter_dependent(source)["node"]
