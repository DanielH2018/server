"""#5658: `FanoutSlotsIdleWithFreeWork` notifies Discord once per idle stretch and never pages.

`test_alertmanager_routing.py` is at its line cap, so the repeat-interval share of this alert's
outcome lives here, on the same walk of the rendered routing tree. The alert dropped `shadow`
on 2026-11-02. It is a warning, so it reaches Discord alone and never email, and its own route
repeats every 24h where the 6h default would send a message every few hours of one idle stretch.

Run: uv run pytest ansible/tests/services/test_alertmanager_routing_fanout_idle.py
"""

from test_alertmanager_routing import (
    PAGING,
    reached,
    repeat_intervals,
    route_tree,
    unmuted,
)

ALERT = "FanoutSlotsIdleWithFreeWork"
LABELS, RECEIVERS = PAGING[f"{ALERT}/daniel-box"]


def test_idle_fanout_slots_notify_discord_alone_once_a_day():
    assert LABELS["severity"] == "warning"
    assert "shadow" not in LABELS
    assert RECEIVERS == ["discord"]
    assert [r["receiver"] for r in reached(route_tree(), LABELS)] == ["discord"]
    assert unmuted(LABELS) == []
    assert repeat_intervals(LABELS) == ["24h"]


def test_idle_fanout_slots_without_their_route_repeat_at_the_default():
    """The rejecting half: the 24h comes from the alert's own route, not from the catch-all."""
    tree = route_tree()
    tree["routes"] = [
        r for r in tree["routes"] if f'alertname="{ALERT}"' not in r.get("matchers", [])
    ]
    assert repeat_intervals(LABELS, tree) == ["6h"]


def test_every_other_alert_keeps_the_repeat_it_had():
    """The route lengthens no other alert's repeat. `DeployHoldActive` keeps its own daily
    route, so it is the one other alert allowed 24h."""
    others = {
        name: labels
        for name, (labels, _) in PAGING.items()
        if labels["alertname"] not in {ALERT, "DeployHoldActive"}
    }
    assert "critical" in others
    for name, labels in others.items():
        expected = ["6h"] * len(reached(route_tree(), labels))
        assert repeat_intervals(labels) == expected, name
