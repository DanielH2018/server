"""Step 6 when `probe.py health` cannot read its store: exit 75 is no verdict (#5543).

PR #5533's landing read `VERDICT: unhealthy` because Prometheus was rolling and Traefik
answered `no available server`, while the workload was `1/1 Running` and a re-run a minute
later read healthy. Only the probe subprocess is faked here: `detach_notify.gate` and
`check_one` run as land.sh runs them, so these grade what the landing does with an unreadable
gate rather than a GateResult this file made up.

Run: uv run pytest scripts/deploy_tools/tests/test_land_health_unreadable_gate.py
"""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from _land_fakes import MERGE_SHA
from deploy_tools.deploy_lib import detach_notify
from deploy_tools.land_lib import health_verdict
from deploy_tools.land_lib.landing import Classification
from deploy_tools.land_lib.outcome import Outcome
from lib.exit_codes import PROBE_UNREADABLE


def _unreadable_landing(landing):
    """A landing of homelab-mcp whose every probe.py health run exits 75."""
    ln, _ = landing()
    ln.merge_sha, ln.resolved_tags = MERGE_SHA, ["homelab-mcp"]
    ln.classification = Classification(
        plane="", self_applied=False, self_applied_command="", remaining_setup=""
    )

    def run(argv, **kwargs):
        return SimpleNamespace(
            returncode=PROBE_UNREADABLE,
            stdout="PROBE-VERDICT: unreadable (probe.py health, exit 75)\n",
            stderr="no available server\n",
        )

    tools = detach_notify.NotifyTools(
        run=run, tag_platforms=lambda tag, host_vars: {"k8s"}
    )
    ln.tools = replace(
        ln.tools,
        gate=lambda tags, cwd=None: detach_notify.gate(
            tags, True, tools=tools, cwd=cwd
        ),
    )
    return ln


def test_a_probe_that_exits_75_does_not_grade_the_landing_unhealthy(landing):
    ln = _unreadable_landing(landing)
    with pytest.raises(Outcome) as exc:
        health_verdict.health(ln)
    assert exc.value.verdict != "unhealthy"


def test_a_gate_that_stays_unreadable_does_not_settle_either(landing):
    """No reading is no proof of health: the landing ends unsettled, with a non-zero exit."""
    ln = _unreadable_landing(landing)
    with pytest.raises(Outcome) as exc:
        health_verdict.health(ln)
    assert exc.value.verdict not in ("unhealthy", "settled")
    assert exc.value.rc != 0
