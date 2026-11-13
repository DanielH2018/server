"""The light and sleep-mode-fan detectors count a write as manual only when a person made it.

An automation started by an MQTT, time or time_pattern trigger runs in a root context, so the Tap
Dial and the wake ramps write with parent_id none, exactly like a person does (#8384). The
discriminator is user_id, which Google Assistant, the dashboard and the companion app set. These
tests render each detector's real condition template against both kinds of context.
"""

from pathlib import Path
from types import SimpleNamespace

import yaml
from jinja_harness import render_template

AUTOMATIONS = Path(__file__).resolve().parent.parent / "files" / "automations"

DETECTORS = {
    "bedroom_manual_light_detect": "lighting.yaml",
    "bedroom_sleep_mode_fan": "wake-and-sleep.yaml",
}

SYSTEM_WRITE = SimpleNamespace(id="ctx", parent_id=None, user_id=None)
PERSON_WRITE = SimpleNamespace(id="ctx", parent_id=None, user_id="a1b2c3")


def _context_gate(automation_id: str) -> str:
    automations = yaml.safe_load((AUTOMATIONS / DETECTORS[automation_id]).read_text())
    found = [a for a in automations if a.get("id") == automation_id]
    assert found, automation_id
    gates = [
        c["value_template"]
        for c in found[0]["condition"]
        if "context" in str(c.get("value_template", ""))
    ]
    assert len(gates) == 1, gates
    return gates[0]


def _passes(automation_id: str, context: SimpleNamespace) -> bool:
    trigger = SimpleNamespace(
        from_state=SimpleNamespace(state="on"),
        to_state=SimpleNamespace(state="off", context=context),
    )
    return render_template(_context_gate(automation_id), trigger=trigger) == "True"


def test_a_root_context_system_write_is_not_manual():
    for automation_id in DETECTORS:
        assert not _passes(automation_id, SYSTEM_WRITE), automation_id


def test_a_dashboard_or_voice_write_is_manual():
    for automation_id in DETECTORS:
        assert _passes(automation_id, PERSON_WRITE), automation_id
