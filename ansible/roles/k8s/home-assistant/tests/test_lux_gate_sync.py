"""Cross-file guard for the auto-light lux gate.

The dusk lux threshold is duplicated as a literal in two places that MUST stay equal:
  - the `auto_light_allowed` macro (custom_templates/lighting.jinja) — `lux < N`, the gate used by
    binary_sensor.bedroom_auto_light_allowed; and
  - bedroom_presence_on's illuminance `numeric_state` trigger (files/automations.yaml) — `below: N`
    (a numeric_state trigger can't call a macro, so the value is inlined).

If they drift, the dusk trigger fires at one threshold while light_decision no-ops at another (a
dead-band where the room dims but the lights don't come on). This test fails on that drift — it
asserts the two literals match WITHOUT pinning the value, so a deliberate retune of both passes.
"""

import re
from pathlib import Path

import yaml

FILES = Path(__file__).resolve().parent.parent / "files"


def _macro_lux_threshold() -> int:
    text = (FILES / "custom_templates" / "lighting.jinja").read_text()
    m = re.search(r"lux < (\d+)", text)
    assert m, "could not find `lux < N` in lighting.jinja (auto_light_allowed)"
    return int(m.group(1))


def _presence_on_trigger_threshold() -> int:
    autos = yaml.safe_load((FILES / "automations.yaml").read_text())
    presence_on = next(a for a in autos if a.get("id") == "bedroom_presence_on")
    illum = next(
        t
        for t in presence_on["trigger"]
        if t.get("entity_id") == "sensor.aqara_fp300_illuminance" and "below" in t
    )
    return int(illum["below"])


def test_lux_gate_threshold_matches_across_files():
    assert _macro_lux_threshold() == _presence_on_trigger_threshold()
