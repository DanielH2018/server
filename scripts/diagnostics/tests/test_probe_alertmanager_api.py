"""`probe.py`'s Alertmanager reads (#6691): the silence or inhibition on a gated alert, and
`monitors`' list of active silences.

Run: uv run pytest scripts/diagnostics/tests/test_probe_alertmanager_api.py
"""

import pytest

from diagnostics.probe_lib import alertmanager_api, core, health_alerts
from lib.exit_codes import PROBE_UNREADABLE

TAG = "littlelink"
SILENCE_ID = "8f2c0d6e-0b1a-4c6e-9a51-3f6f2b7a9c10"
INHIBITOR = "4a1b2c3d4e5f6a7b"


def _firing_critical(promql):
    """A Prometheus holding one gated alert of TAG with no readable activation time."""
    if promql.startswith("ALERTS{") and 'severity="critical"' in promql:
        labels = {
            "alertname": "PodCrashLooping",
            "severity": "critical",
            "service": TAG,
        }
        return [{"metric": {"__name__": "ALERTS", **labels}, "value": [0, "1"]}]
    return []


def _am_alert(silenced=(), inhibited=(), **labels):
    labels = {
        "alertname": "PodCrashLooping",
        "severity": "critical",
        "service": TAG,
        **labels,
    }
    return {
        "labels": labels,
        "status": {
            "state": "suppressed" if silenced or inhibited else "active",
            "silencedBy": list(silenced),
            "inhibitedBy": list(inhibited),
        },
    }


def _gate(silences):
    tools = health_alerts.PromTools(
        query=_firing_critical, sleep=lambda s: None, silences=silences
    )
    return health_alerts.gate(TAG, None, 0.0, has_route=False, tools=tools)


def test_a_silenced_gated_alert_is_named_and_still_fails_the_gate():
    code, lines = _gate(lambda tag: [_am_alert(silenced=[SILENCE_ID])])
    assert code == 1, lines
    named = [line for line in lines if SILENCE_ID in line]
    assert named and "silenced by" in named[0], lines


def test_an_inhibited_gated_alert_names_its_inhibitor():
    code, lines = _gate(lambda tag: [_am_alert(inhibited=[INHIBITOR])])
    assert code == 1, lines
    assert any(f"inhibited by {INHIBITOR}" in line for line in lines), lines


def test_a_muted_alert_outside_the_gated_set_is_not_named():
    shadow = _am_alert(silenced=[SILENCE_ID], shadow="true")
    warning = _am_alert(silenced=[SILENCE_ID], severity="warning")
    assert alertmanager_api.suppression_lines([shadow, warning]) == []
    assert alertmanager_api.suppression_lines([_am_alert(silenced=[SILENCE_ID])])


def test_an_unread_alertmanager_is_a_note_and_leaves_the_verdict():
    def unreadable(tag):
        raise core.Unreadable("curl: (22) 404")

    code, lines = _gate(unreadable)
    assert code == 1, lines
    assert any("Alertmanager unread" in line for line in lines), lines


def test_the_gate_reads_alertmanager_for_its_own_tag():
    asked = []
    _gate(lambda tag: asked.append(tag) or [])
    assert asked == [TAG]
    assert "filter=service%3D%22littlelink%22" in alertmanager_api.alerts_url(
        "https://x", TAG
    )


def _silence(sid, state, **extra):
    return {
        "id": sid,
        "status": {"state": state},
        "matchers": [
            {
                "name": "alertname",
                "value": "PodCrashLooping",
                "isRegex": False,
                "isEqual": True,
            },
            {"name": "service", "value": "little.*", "isRegex": True, "isEqual": False},
        ],
        "endsAt": "2026-11-01T00:00:00.000Z",
        "createdBy": "daniel",
        "comment": "planned window",
        **extra,
    }


def test_monitors_lists_an_active_silence_and_skips_an_expired_one():
    lines, code = alertmanager_api.format_silences(
        [_silence(SILENCE_ID, "active"), _silence("expired-one", "expired")]
    )
    assert code == 0
    (line,) = lines
    assert SILENCE_ID in line
    assert 'alertname="PodCrashLooping"' in line and 'service!~"little.*"' in line
    assert "daniel" in line and "planned window" in line


@pytest.mark.parametrize(
    "fetched, monitors_code, expected",
    [
        ([_silence(SILENCE_ID, "active")], 0, 0),
        ("not json", 0, PROBE_UNREADABLE),
        ("not json", 1, 1),
    ],
)
def test_report_folds_an_unread_alertmanager_into_the_exit_code(
    capsys, fetched, monitors_code, expected
):
    def silences():
        if isinstance(fetched, str):
            raise core.Unreadable(fetched)
        return fetched

    assert alertmanager_api.report(monitors_code, silences) == expected
    if expected == 0:
        assert SILENCE_ID in capsys.readouterr().out


def test_a_body_that_is_not_a_list_is_unreadable():
    with pytest.raises(core.Unreadable):
        alertmanager_api.read(
            "https://x/api/v2/silences", None, fetch=lambda u, resolve: {"code": 404}
        )
