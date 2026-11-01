"""`probe.py monitors`: what is down, from `ALERTS` and the Kuma tiles not yet cut over."""

from diagnostics.probe_lib import monitors
from lib.exit_codes import PROBE_UNREADABLE

WATCHDOG = {"alertname": "Watchdog", "service": "observability", "severity": "info"}


def _vector(*label_sets, value="1"):
    result = [{"metric": labels, "value": [0, value]} for labels in label_sets]
    return {"status": "success", "data": {"result": result}}


def _alert(name, service, severity, **extra):
    return {"alertname": name, "service": service, "severity": severity, **extra}


def test_only_the_watchdog_firing_is_clean():
    lines, code = monitors.format_alerts(_vector(WATCHDOG))
    assert code == 0
    assert lines == ["0 paging alert(s) firing"]


def test_a_firing_critical_alert_is_flagged_under_its_severity_and_service():
    data = _vector(WATCHDOG, _alert("IngressProbeFailed", "jellyfin", "critical"))
    lines, code = monitors.format_alerts(data)
    assert code == 1
    assert lines == [
        "1 paging alert(s) firing",
        "critical:",
        "  jellyfin: IngressProbeFailed",
    ]


def test_a_shadow_alert_is_listed_and_not_counted():
    shadow = _alert("EvalSweepFailed", "daniel-box", "warning", shadow="true")
    lines, code = monitors.format_alerts(_vector(WATCHDOG, shadow))
    assert code == 0
    assert "  daniel-box: EvalSweepFailed{severity=warning}" in lines
    assert lines[0] == "0 paging alert(s) firing"


def test_an_info_alert_is_a_note_and_not_counted():
    info = _alert("CertExpiringSoon", "traefik", "info")
    lines, code = monitors.format_alerts(_vector(WATCHDOG, info))
    assert code == 0
    assert "notes (not paging):" in lines


def test_no_watchdog_is_unreadable_rather_than_clean():
    """An empty `ALERTS` is also what a dead rule evaluator returns (failure class 3)."""
    lines, code = monitors.format_alerts(_vector())
    assert code == PROBE_UNREADABLE
    assert lines[0].startswith("Watchdog is not firing")


def test_an_unread_alerts_response_is_unreadable():
    lines, code = monitors.format_alerts("curl: (7) Failed to connect")
    assert code == PROBE_UNREADABLE
    assert lines == ["  ALERTS unread: curl: (7) Failed to connect"]


def test_every_kuma_tile_up_is_clean():
    assert monitors.format_kuma(_vector()) == ([], 0)


def test_a_kuma_tile_not_up_is_flagged_by_name():
    lines, code = monitors.format_kuma(
        _vector({"monitor_name": "Pi SD Health"}, value="0")
    )
    assert code == 1
    assert lines[-1] == "  Pi SD Health: DOWN"


def test_a_problem_outranks_an_unread_source():
    assert monitors.combine(PROBE_UNREADABLE, 1) == 1
    assert monitors.combine(0, PROBE_UNREADABLE) == PROBE_UNREADABLE
    assert monitors.combine(0, 0) == 0
