"""Step 6 when `probe.py health` cannot read its store: exit 75 is no verdict (#5543).

PR #5533's landing read `VERDICT: unhealthy` because Prometheus was rolling and Traefik
answered `no available server`, while the workload was `1/1 Running` and a re-run a minute
later read healthy. Only the probe subprocess is faked here: `detach_notify.gate` and
`check_one` run as land.sh runs them, so these grade what the landing does with an unreadable
gate rather than a GateResult this file made up.

Run: uv run pytest scripts/deploy_tools/tests/test_land_health_unreadable_gate.py
"""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from _land_fakes import MERGE_SHA
from deploy_tools.deploy_lib import detach_notify
from deploy_tools.land_lib import outcome
from deploy_tools.land_lib import health_verdict, land_probe, ledger
from deploy_tools.land_lib.landing import Classification
from deploy_tools.land_lib.outcome import Outcome
from diagnostics.probe_lib import health_checkout
from lib.exit_codes import PROBE_UNREADABLE


def _unreadable_landing(landing, stderr="no available server\n"):
    """A landing of homelab-mcp whose every probe.py health run exits 75, saying `stderr`."""
    ln, _ = landing()
    ln.merge_sha, ln.flux_tags = MERGE_SHA, ["homelab-mcp"]
    ln.classification = Classification(plane="")

    def run(argv, **kwargs):
        return SimpleNamespace(
            returncode=PROBE_UNREADABLE,
            stdout="PROBE-VERDICT: unreadable (probe.py health, exit 75)\n",
            stderr=stderr,
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


def test_a_store_that_does_not_answer_defers_with_the_store_wording(landing, capsys):
    ln = _unreadable_landing(landing)
    with pytest.raises(Outcome) as exc:
        health_verdict.health(ln)
    assert (exc.value.rc, exc.value.verdict) == (75, "deferred")
    assert "could not read its store" in exc.value.detail
    assert outcome.STALE_GATE_NOTE not in capsys.readouterr().out


def test_a_stale_gate_tree_defers_without_blaming_the_store(landing, capsys):
    """#7816: a re-run from a tree master has moved past refuses again, so the Prometheus
    advice sent the operator into a loop."""
    stale = (
        f"homelab-mcp: {health_checkout.STALE_CHECKOUT}: origin/master changed the k8s role "
        "'homelab-mcp' at 4f2204fdb1, after this checkout's HEAD\n"
    )
    ln = _unreadable_landing(landing, stderr=stale)
    with pytest.raises(Outcome) as exc:
        health_verdict.health(ln)
    out = capsys.readouterr().out
    assert (exc.value.rc, exc.value.verdict) == (75, "deferred")
    assert "older than origin/master" in exc.value.detail
    assert "could not read its store" not in exc.value.detail + out
    assert outcome.STALE_GATE_NOTE in out
    assert outcome.UNREADABLE_GATE_NOTE not in out


_STALE_STDERR = (
    f"homelab-mcp: {health_checkout.STALE_CHECKOUT}: origin/master changed the k8s role "
    "'homelab-mcp' at 4f2204fdb1, after this checkout's HEAD\n"
)


def _gate_give_up_cause(landing, stderr: str, logs: Path) -> tuple[str, Path]:
    """The ledger cause the gate deferral records, and a log of it carrying no note."""
    ln = _unreadable_landing(landing, stderr=stderr)
    with pytest.raises(Outcome) as exc:
        health_verdict.health(ln)
    logs.mkdir()
    log = logs / "land999-20261111-120000.log"
    log.write_text(
        f"{ledger.result_line(ln.ledger, exc.value)}\nVERDICT: deferred ({exc.value.detail})\n"
    )
    log.with_suffix(".rc").write_text(f"{exc.value.rc}\n")
    return ln.ledger.cause, log


@pytest.mark.parametrize(
    "stderr", ["no available server\n", _STALE_STDERR], ids=["unreadable", "stale"]
)
def test_a_gate_deferral_records_a_cause_that_reads_gave_up_without_its_note(
    landing, stderr, tmp_path
):
    """#7911: a reworded note must not turn a gate give-up into a plain deferral."""
    cause, log = _gate_give_up_cause(landing, stderr, tmp_path / "logs")
    assert cause in outcome.CAUSES and cause != outcome.Cause.INVALID
    assert land_probe.read(log)["state"] == "gave-up"


def test_an_unreadable_gate_and_a_stale_gate_record_different_causes(landing, tmp_path):
    """The Landings board groups `by (cause)`, so the two give-ups need two bars."""
    unreadable, _ = _gate_give_up_cause(
        landing, "no available server\n", tmp_path / "unreadable"
    )
    stale, _ = _gate_give_up_cause(landing, _STALE_STDERR, tmp_path / "stale")
    assert unreadable and stale and unreadable != stale
