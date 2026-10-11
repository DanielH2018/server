"""The `land` source cc-wait reads: `land_probe.py` and the `.claude/wait-sources/land` shim.

Run: uv run pytest scripts/deploy_tools/tests/test_land_probe.py

The probe's terminal states must satisfy cc-wait's contract (dotfiles
docs/specs/2026-10-04-cc-wait-design.md), which this repo's CI cannot import: no reserved code
(2 or 75) and at least one failure state. `test_the_declared_states_keep_cc_wait_s_contract`
restates those two rules here so a drift fails in this repo rather than at a session's wait.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

from deploy_tools.land_lib import land_probe
from deploy_tools.land_lib.outcome import ABANDONED_WATCH_NOTE

_SHIM = Path(__file__).resolve().parents[3] / ".claude" / "wait-sources" / "land"


def landing(
    tmp_path: Path, text: str, rc: int | None = None, pid: int | None = None
) -> Path:
    log = tmp_path / "land939-20261004-120000.log"
    log.write_text(text)
    if rc is not None:
        log.with_suffix(".rc").write_text(f"{rc}\n")
    if pid is not None:
        log.with_suffix(".pid").write_text(f"{pid}\n")
    return log


@pytest.fixture
def dead_pid():
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def test_a_landing_that_gave_up_ends_the_wait_with_3_not_75(tmp_path):
    """75 is cc-wait's "re-run this wait"; land.sh's own 75 means "re-run land.sh"."""
    log = landing(tmp_path, "VERDICT: merge-timeout (PR #939)\n", rc=75)
    assert land_probe.read(log)["state"] == "gave-up"
    assert land_probe.TERMINAL["gave-up"] == 3


def test_a_plain_deferral_ends_the_wait_as_deferred_not_gave_up(tmp_path):
    """`deferred` is not a resume point: the next tick applies the PR (issue #3932)."""
    log = landing(
        tmp_path, "VERDICT: deferred (PR #939 — landed, not yet applied)\n", rc=75
    )
    assert land_probe.read(log)["state"] == "deferred"
    assert land_probe.TERMINAL["deferred"] == 4


def test_a_deferral_that_abandoned_a_mid_apply_tick_still_gave_up(tmp_path):
    """The abandoned-watch note means a hold cannot be ruled out: re-run land.sh."""
    log = landing(
        tmp_path,
        f"VERDICT: deferred (PR #939 — landed, not yet applied)\n{ABANDONED_WATCH_NOTE}\n",
        rc=75,
    )
    assert land_probe.read(log)["state"] == "gave-up"


def test_a_refusal_without_a_verdict_reports_its_land_line(tmp_path):
    """A landing-policy `die()` writes no VERDICT line; its reason is the last `land:` line."""
    log = landing(
        tmp_path,
        "land: warning the landing carried on past\n"
        "land: the body closes #12, an issue it does not fix\n",
        rc=1,
    )
    state = land_probe.read(log)
    assert state["state"] == "failed"
    assert state["detail"].startswith(
        "land: the body closes #12, an issue it does not fix; read "
    )


def test_a_multi_line_refusal_is_reported_whole(tmp_path):
    """`_refuse_stray_closing_refs` lists the refs and the remedy below its first line."""
    message = (
        "land: PR #939's body carries a closing keyword (issue #2513):\n"
        "  Fixes #12 in passing\n"
        "Rewrite the reference, then re-run this."
    )
    log = landing(tmp_path, f"== arm  arming\n{message}\n", rc=1)
    assert land_probe.read(log)["detail"] == f"{message}; read {log}"


def test_a_land_warning_ends_at_the_next_phase(tmp_path):
    log = landing(tmp_path, "land: a warning\n== 4/6  deploying\n", rc=1)
    assert land_probe.read(log)["detail"] == f"land: a warning; read {log}"


def test_a_landing_with_neither_line_says_so(tmp_path):
    log = landing(tmp_path, "== 0/6  waiting\n", rc=1)
    assert land_probe.read(log)["detail"] == f"no VERDICT line; read {log}"


def test_a_running_landing_reports_its_newest_phase(tmp_path):
    log = landing(
        tmp_path, "== arm  arming\n== 0/6  waiting for PR #939 to merge\n  merged\n"
    )
    assert land_probe.read(log) == {
        "state": "running",
        "detail": "0/6 waiting for PR #939 to merge",
    }


def test_a_landing_whose_pid_died_without_a_code_died(tmp_path, dead_pid):
    log = landing(tmp_path, "== 4/6  deploying\n", pid=dead_pid)
    assert land_probe.read(log)["state"] == "died"


def test_a_live_landing_without_a_code_is_still_running(tmp_path):
    log = landing(tmp_path, "== 4/6  deploying\n", pid=os.getpid())
    assert land_probe.read(log)["state"] == "running"


def test_the_newest_log_for_the_pr_is_the_landing(tmp_path):
    (tmp_path / "land939-20261004-090000.log").write_text("old\n")
    newest = tmp_path / "land939-20261004-120000.log"
    newest.write_text("new\n")
    (tmp_path / "land9390-20261004-130000.log").write_text("another PR\n")
    assert land_probe.find_log("939", "", str(tmp_path)) == newest


def test_no_log_for_the_pr_is_refused(tmp_path):
    with pytest.raises(land_probe.NoLanding):
        land_probe.find_log("939", "", str(tmp_path))


def test_the_declared_states_keep_cc_wait_s_contract():
    codes = land_probe.TERMINAL.values()
    assert not {2, 75} & set(codes)
    assert any(code != 0 for code in codes)
    # Every code land.sh documents ends the wait in a declared state.
    assert set(land_probe._STATE_BY_RC.values()) <= set(land_probe.TERMINAL)


def test_the_shim_runs_the_probe_and_prints_one_json_state(tmp_path):
    """The transport cc-wait actually uses: an executable, its argv, one JSON line out."""
    landing(tmp_path, "VERDICT: settled (PR #939)\n", rc=0)
    out = subprocess.run(
        [str(_SHIM), "939", "--log-dir", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    assert json.loads(out.stdout) == {
        "state": "landed",
        "detail": "VERDICT: settled (PR #939)",
    }
