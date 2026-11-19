"""Tests for `probe.py unit`.

The documents are in the shape `host_reader.READER` returns, with the values daniel-box's
`homelab-builder` units held on 2026-11-15.
"""

from types import SimpleNamespace


import pytest
from diagnostics.probe_lib import core, host_unit
from lib.exit_codes import PROBE_FAILED, PROBE_OK, PROBE_UNREADABLE

NOW = 1794777200.0
BASE = "homelab-builder"
NEXT_US = 1794777213742741
ENTRIES = [
    [1794777093.0, 6, "r1", "systemd", "Starting homelab-builder.service - Homelab builder..."],
    [1794777100.8, 6, "r1", "env", "homelab-builder: 5c4b15943b13 changes no package"],
    [1794777101.0, 6, "r1", "systemd", "homelab-builder.service: Deactivated successfully."],
    [1794777101.0, 6, "r1", "systemd", "Finished homelab-builder.service - Homelab builder."],
]  # fmt: skip


def _read(entries=ENTRIES, rc=0, stderr=""):
    return {"rc": rc, "stderr": stderr, "entries": entries}


def _doc(
    result="success", load="loaded", timer_load="loaded", next_us=NEXT_US, journal=None
):
    return {
        "host": "daniel-box",
        "user": "claude",
        "show": {
            f"{BASE}.service": {
                "LoadState": load,
                "ActiveState": "inactive",
                "SubState": "dead",
                "Result": result,
                "ExecMainStartTimestamp": "Sun 2026-11-15 21:11:34 UTC",
                "ExecMainExitTimestamp": "Sun 2026-11-15 21:11:41 UTC",
                "ExecMainStatus": "0",
            },
            f"{BASE}.timer": {"LoadState": timer_load, "ActiveState": "active"},
        },
        "timers": {f"{BASE}.timer": {"next": next_us, "last": 1794777093731191}},
        "journal": journal
        or {"unit": _read(), "tag:homelab-builder": _read(ENTRIES[1:2])},
    }


def _report(doc, last=10):
    return host_unit.report(doc, BASE, now=NOW, last=last)


def test_a_unit_whose_last_run_succeeded_and_whose_timer_will_fire_is_ok():
    text, code = _report(_doc())
    assert code == PROBE_OK
    assert "result success" in text
    assert "last start Sun 2026-11-15 21:11:34 UTC" in text
    assert "next 2026-11-15 21:13:33 UTC" in text
    assert text.endswith("verdict: ok")


def test_a_failed_last_run_is_the_fail_verdict():
    text, code = _report(_doc(result="exit-code"))
    assert code == PROBE_FAILED
    assert "verdict: the last run ended exit-code" in text


def test_a_unit_that_is_not_installed_fails_and_names_its_load_state():
    text, code = _report(_doc(load="not-found"))
    assert code == PROBE_FAILED
    assert "homelab-builder.service is not-found on daniel-box" in text


def test_a_loaded_timer_with_no_next_trigger_fails():
    text, code = _report(_doc(next_us=0))
    assert code == PROBE_FAILED
    assert "has no next trigger" in text


def test_a_unit_with_no_timer_is_judged_on_its_service_alone():
    text, code = _report(_doc(timer_load="not-found"))
    assert code == PROBE_OK
    assert f"{BASE}.timer: none" in text


def test_routine_systemd_lines_drop_and_a_line_in_two_reads_prints_once():
    text, _ = _report(_doc())
    assert text.count("changes no package") == 1
    assert "Deactivated successfully" not in text and "Starting" not in text
    assert "journal, newest 1 non-routine lines:" in text


def test_a_journal_this_user_cannot_read_prints_the_state_and_exits_unreadable():
    hidden = _read(
        [], stderr="Hint: You are currently not seeing messages from other users"
    )
    text, code = _report(_doc(journal={"unit": hidden}))
    assert code == PROBE_UNREADABLE
    assert "result success" in text and "unread: journalctl for unit" in text


def test_the_read_covers_the_unit_both_forms_of_its_tag_and_systemds_lines():
    plan = host_unit.plan("homelab-aide-check", host_unit.tags("homelab-aide-check"))
    assert plan["journal"]["unit"][:1] == ["homelab-aide-check.service"]
    assert {v[0] for k, v in plan["journal"].items() if k.startswith("tag:")} == {
        "aide-check",
        "homelab-aide-check",
    }
    assert plan["timers"] == ["homelab-aide-check.timer"]
    assert plan["journal"]["systemd"][:1] == ["UNIT=homelab-aide-check.service"]


def test_a_unit_name_is_trimmed_of_its_suffix_and_an_option_is_refused():
    assert host_unit.base_name("homelab-updater.timer") == "homelab-updater"
    with pytest.raises(core.UsageError):
        host_unit.base_name("--all")


def test_the_dry_run_names_the_read_and_makes_none(capsys):
    def no_read(*_a, **_k):
        raise AssertionError("a dry run must not read")

    ns = SimpleNamespace(unit=BASE, host="daniel-pi", last=10, dry_run=True)
    assert host_unit.run_unit(ns, read=no_read) == PROBE_OK
    assert "homelab-builder.timer" in capsys.readouterr().out


def test_a_running_service_explains_a_monotonic_timer_with_no_next_trigger():
    doc = _doc(next_us=0)
    doc["show"][f"{BASE}.service"] |= {"ActiveState": "activating", "SubState": "start"}
    text, code = _report(doc)
    assert code == PROBE_OK
    assert "next after the running service ends" in text


def _reset(doc):
    """The state systemd reports for a unit it reloaded: Result success, no timestamps."""
    doc["show"][f"{BASE}.service"] |= {
        "ExecMainStartTimestamp": "",
        "ExecMainExitTimestamp": "",
    }
    return doc


def test_a_reset_unit_takes_its_last_result_from_the_journal():
    failed = [
        1794777101.0,
        3,
        "r1",
        "systemd",
        f"{BASE}.service: Failed with result 'exit-code'.",
    ]
    doc = _reset(_doc(journal={"unit": _read([*ENTRIES[:2], failed])}))
    text, code = _report(doc)
    assert code == PROBE_FAILED
    assert "result exit-code (from the journal)" in text


def test_a_reset_unit_with_no_result_in_the_journal_reads_unknown_not_ok():
    doc = _reset(_doc(journal={"unit": _read(ENTRIES[1:2])}))
    text, code = _report(doc)
    assert code == PROBE_UNREADABLE
    assert "result unknown (systemd holds no run)" in text
    assert text.endswith("verdict: result unknown")
