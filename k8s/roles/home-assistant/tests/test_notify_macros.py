"""Unit tests for the away-hold macros in custom_templates/notify.jinja, and for the templates
that read the hold store they maintain."""

import json
from pathlib import Path

import yaml
from jinja_harness import render_macro, render_template

NOTIFY = "notify.jinja"
FILES = Path(__file__).resolve().parent.parent / "files"

HELD = {
    "window_advice": {"title": "Open a window?", "message": "Air's stale"},
    "update_digest": {"title": "2 updates", "message": "core, zigbee"},
}


def _route(away, alert_class, pierce=False, recovery=False):
    return render_macro(NOTIFY, "notify_route", away, alert_class, pierce, recovery)


def _after(held, **event):
    return json.loads(render_macro(NOTIFY, "held_after", held, event))


def _flush():
    automations = yaml.safe_load((FILES / "automations" / "alerts.yaml").read_text())
    return next(a for a in automations if a["id"] == "bedroom_flush_held_notifications")


def _store(held):
    return lambda entity_id, attribute: {
        ("sensor.bedroom_held_alerts", "held"): held
    }.get((entity_id, attribute))


def test_an_alert_not_declared_able_to_wait_pushes_while_away():
    # The last two are an alert with no class and a misspelt one.
    for alert_class in ("power", "safety", "alert_chain", "away_only", "", "comfrot"):
        assert _route(True, alert_class) == "push"
    assert _route(True, "power", recovery=True) == "push"


def test_an_alert_that_can_wait_is_held_while_away():
    assert _route(True, "comfort") == "hold"
    assert _route(True, "upkeep") == "hold"


def test_nothing_is_held_at_home():
    assert _route(False, "comfort") == "push"
    assert _route(False, "comfort", recovery=True) == "push"


def test_pierce_pushes_an_alert_that_could_wait():
    assert _route(True, "comfort", pierce=True) == "push"


def test_a_recovery_while_away_cancels_the_held_alert():
    assert _route(True, "comfort", recovery=True) == "cancel"


def test_hold_adds_an_alert_and_replaces_one_with_the_same_tag():
    added = _after(
        HELD, action="hold", tag="humidity", title="Humidity", message="71 %"
    )
    assert added == HELD | {"humidity": {"title": "Humidity", "message": "71 %"}}
    replaced = _after(
        HELD, action="hold", tag="update_digest", title="3 updates", message="more"
    )
    assert replaced["update_digest"] == {"title": "3 updates", "message": "more"}
    assert len(replaced) == 2


def test_hold_starts_from_empty_when_the_store_has_no_state_yet():
    assert _after(None, action="hold", tag="t", title="T", message="m") == {
        "t": {"title": "T", "message": "m"}
    }


def test_cancel_removes_one_tag_and_ignores_an_absent_one():
    assert _after(HELD, action="cancel", tag="window_advice") == {
        "update_digest": HELD["update_digest"]
    }
    assert _after(HELD, action="cancel", tag="never_held") == HELD


def test_flushed_removes_only_the_delivered_tags():
    assert _after(HELD, action="flushed", tags=["window_advice"]) == {
        "update_digest": HELD["update_digest"]
    }


def test_the_digest_lists_each_held_alert_on_its_own_line():
    assert render_macro(NOTIFY, "held_digest", HELD) == (
        "• Open a window?: Air's stale\n• 2 updates: core, zigbee"
    )


def test_the_arrival_flush_runs_when_the_store_holds_an_alert():
    condition = _flush()["condition"][0]["value_template"]
    assert render_template(condition, state_attr=_store(HELD)) == "True"


def test_the_arrival_flush_stays_silent_on_an_empty_or_missing_store():
    condition = _flush()["condition"][0]["value_template"]
    assert render_template(condition, state_attr=_store({})) == "False"
    assert render_template(condition, state_attr=_store(None)) == "False"


def test_the_arrival_flush_empties_the_store_only_after_its_push():
    steps = _flush()["action"]
    push = next(i for i, step in enumerate(steps) if "service" in step)
    flushed = next(i for i, step in enumerate(steps) if "event" in step)
    assert steps[push]["service"].startswith("notify.")
    assert "continue_on_error" not in steps[push]
    assert flushed > push
