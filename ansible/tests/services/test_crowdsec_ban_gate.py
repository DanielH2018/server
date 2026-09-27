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


def _bouncers(
    live_pull: str | None, rc: int = 0, new_pod_pull: str | None = None
) -> dict:
    """`cscli bouncers list -o json` as LAPI returns it on daniel-box.

    The base `k8straefik` row is stale: LAPI records each Traefik pod's pulls on an
    auto-created `k8straefik@<pod IP>` row. The gate's first live run compared only the base
    row and waited out a pull that had happened (#2752).
    """
    rows = [
        {"name": "k8straefik", "last_pull": "2026-08-09T14:11:31.542634271Z"},
        {
            "name": "k8straefik@10.42.0.118",
            "last_pull": "2026-09-27T07:36:07.091402493Z",
        },
        {"name": "k8straefik@10.42.0.207", "last_pull": live_pull},
        {"name": "dockertraefik", "last_pull": None},
        {"name": "k8straefik-lookalike", "last_pull": "2026-09-27T13:59:00Z"},
    ]
    if new_pod_pull:
        rows.append({"name": "k8straefik@10.42.0.31", "last_pull": new_pod_pull})
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


_BEFORE = _bouncers("2026-09-27T13:32:24.382113004Z")


@pytest.mark.parametrize(
    "now",
    [
        pytest.param(
            _bouncers("2026-09-27T13:33:24.383283726Z"), id="live-pod-row-pulled"
        ),
        pytest.param(
            _bouncers(
                "2026-09-27T13:32:24.382113004Z", new_pod_pull="2026-09-27T13:33:40.1Z"
            ),
            id="restarted-pod-new-row-pulled",
        ),
    ],
)
def test_a_pull_after_the_ban_releases_the_wait(now: dict) -> None:
    assert _pulled(_BEFORE, now) is True


@pytest.mark.parametrize(
    "now",
    [
        pytest.param(
            _bouncers("2026-09-27T13:32:24.382113004Z"), id="no-pull-since-the-ban"
        ),
        pytest.param(_bouncers(None, rc=1), id="cscli-failed"),
    ],
)
def test_no_pull_after_the_ban_holds_the_wait(now: dict) -> None:
    # `k8straefik-lookalike` pulled recently in every fixture, and must not count: only the
    # edge bouncer's own rows carry the ban to Traefik.
    assert _pulled(_BEFORE, now) is False


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
