"""zigbee_bridge_offline sends "back online" only after an outage the offline alert reported."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from jinja_harness import render_template

SYSTEM = (
    Path(__file__).resolve().parent.parent / "files" / "automations" / "system.yaml"
)


def _recovery_gate() -> str:
    automations = yaml.safe_load(SYSTEM.read_text())
    bridge = next(a for a in automations if a.get("id") == "zigbee_bridge_offline")
    recovery = bridge["action"][0]["default"][0]
    return recovery.get("if", "{{ true }}")  # an unconditional notify always fires


def _notifies(outage_seconds: float) -> bool:
    went_off = datetime(2026, 11, 20, 13, 28, tzinfo=UTC)
    trigger = SimpleNamespace(
        id="recovery",
        from_state=SimpleNamespace(state="off", last_changed=went_off),
        to_state=SimpleNamespace(
            state="on", last_changed=went_off + timedelta(seconds=outage_seconds)
        ),
    )
    rendered = render_template(
        _recovery_gate(), trigger=trigger, as_timestamp=lambda dt: dt.timestamp()
    )
    return rendered == "True"


@pytest.mark.parametrize("outage_seconds", [5, 32, 119])
def test_a_flap_shorter_than_the_offline_delay_sends_no_recovery(outage_seconds):
    assert not _notifies(outage_seconds)


@pytest.mark.parametrize("outage_seconds", [120, 130])
def test_an_outage_the_offline_alert_reported_sends_a_recovery(outage_seconds):
    assert _notifies(outage_seconds)
