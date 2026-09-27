"""crowdsec's edge ban gate waits for the bouncer's pull and always lifts its ban.

#2752: the gate banned daniel-pi, then probed the edge on a fixed 80s timer. The Traefik bouncer
made no stream pull for 600s (the plugin's metrics-ticker stall), so every probe saw 302 and
the gate failed with nothing naming why. The failure also skipped the lift, which left the Pi
banned at the edge for the rest of the decision's life. The gate now waits on the bouncer's
LAPI `last_pull` moving past its post-ban value before it probes, and lifts the ban in the
block's `always`. These tests read the tasks as written in roles/k8s/crowdsec/tasks/verify.yml
and evaluate the wait's `until` through Ansible's own templar, because a wrong filter chain
there reads as a stall on every deploy.
"""

import json

import pytest
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template
from lib import yaml_fast

from _helpers import ROLES

_VERIFY = ROLES / "k8s" / "crowdsec" / "tasks" / "verify.yml"


def _gate_block() -> dict:
    tasks = yaml_fast.safe_load(_VERIFY.read_text())
    blocks = [t for t in tasks if "block" in t]
    # Non-vacuity: the tests below read this block, so its absence must fail loudly.
    assert len(blocks) == 1, (
        f"expected one gate block in {_VERIFY}, found {len(blocks)}"
    )
    return blocks[0]


def _task(tasks: list[dict], prefix: str) -> dict:
    matches = [t for t in tasks if t["name"].startswith(prefix)]
    assert len(matches) == 1, (
        f"expected one task named {prefix!r}, found {len(matches)}"
    )
    return matches[0]


def _bouncers(last_pull: str | None, rc: int = 0) -> dict:
    rows = [
        {"name": "k8straefik", "last_pull": last_pull},
        {"name": "some-other-bouncer", "last_pull": "2026-09-27T12:40:00Z"},
    ]
    return {"rc": rc, "stdout": json.dumps(rows) if rc == 0 else ""}


def _pulled(before: dict, now: dict) -> bool:
    until = _task(_gate_block()["block"], "Wait for the edge bouncer")["until"]
    templar = Templar(
        loader=DataLoader(),
        variables={
            "crowdsec_k8s_gate_bouncers_before": before,
            "crowdsec_k8s_gate_bouncers": now,
        },
    )
    return templar.template(trust_as_template("{{ " + until + " }}"))


def test_a_pull_after_the_ban_releases_the_wait() -> None:
    before = _bouncers("2026-09-27T12:22:24.4Z")
    assert _pulled(before, _bouncers("2026-09-27T12:23:24.4Z")) is True


@pytest.mark.parametrize(
    "now",
    [
        pytest.param(_bouncers("2026-09-27T12:22:24.4Z"), id="no-pull-since-the-ban"),
        pytest.param(_bouncers(None, rc=1), id="cscli-failed"),
    ],
)
def test_no_pull_after_the_ban_holds_the_wait(now: dict) -> None:
    # The other bouncer's pull must not count: only the edge bouncer fetches the ban.
    assert _pulled(_bouncers("2026-09-27T12:22:24.4Z"), now) is False


def test_the_probe_runs_only_after_the_pull_wait() -> None:
    names = [t["name"] for t in _gate_block()["block"]]
    wait = names.index(
        _task(_gate_block()["block"], "Wait for the edge bouncer")["name"]
    )
    probe = names.index(
        _task(_gate_block()["block"], "Probe the VIP from the banned Pi")["name"]
    )
    assert wait < probe


def test_the_ban_is_lifted_whether_or_not_the_gate_passes() -> None:
    block = _gate_block()
    _task(block["block"], "Ban daniel-pi")
    lift = _task(block.get("always") or [], "Lift the test ban")
    assert "cscli decisions delete" in lift["ansible.builtin.command"]["cmd"]
