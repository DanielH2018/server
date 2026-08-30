"""The backfill timer's unit and the harness it runs must agree about exit codes and flags.

The unit is a systemd template and the harness is a Python script, so nothing but a test keeps
them in step. Both failures here are silent in the direction that matters: a `SuccessExitStatus`
that no longer matches `CONDITION_NOT_MET` makes the unit page every hour for weeks — the
expected state while the streak is short — and an operator learns to ignore it before it ever
means anything. A missing flag makes the run a no-op that still exits 0.
"""

import pathlib
import re

_REPO = pathlib.Path(__file__).resolve().parents[2]
_UNIT = (
    _REPO / "ansible/roles/setup/gitops_deploy/templates/staging-backfill.service.j2"
)
_TIMER = _REPO / "ansible/roles/setup/gitops_deploy/templates/staging-backfill.timer.j2"
_TASKS = _REPO / "ansible/roles/setup/gitops_deploy/tasks/main.yml"
_HARNESS = _REPO / "scripts/deploy_tools/backfill_staging_gate.py"


def harness_constant(name: str) -> int:
    match = re.search(rf"^{name} = (\d+)$", _HARNESS.read_text(), re.M)
    assert match, f"{_HARNESS.name} no longer defines {name}"
    return int(match.group(1))


def test_the_units_tolerated_exit_is_the_harnesss_not_met_code():
    """NOT MET is the expected state for weeks, so it must not read as a failed unit."""
    tolerated = re.search(r"^SuccessExitStatus=(\d+)$", _UNIT.read_text(), re.M)
    assert tolerated, "the unit no longer tolerates any exit code"
    assert int(tolerated.group(1)) == harness_constant("CONDITION_NOT_MET")


def test_the_unit_does_not_tolerate_the_could_not_run_code():
    """The rejecting half, and the reason the harness splits three ways rather than two.

    A unit that also swallowed COULD_NOT_RUN would stay green through a harness that cannot
    run at all — the ratchet silently stopping, which looks identical to a quiet week.
    """
    tolerated = re.search(r"^SuccessExitStatus=(\d+)$", _UNIT.read_text(), re.M)
    assert int(tolerated.group(1)) != harness_constant("COULD_NOT_RUN")


def test_the_unit_passes_the_flags_the_scheduled_form_needs():
    # --since-ledger without --jsonl exits COULD_NOT_RUN, so the pair is the contract.
    exec_start = _UNIT.read_text()
    for flag in ("--since-ledger", "--jsonl", "--count", "--timeout"):
        assert flag in exec_start, f"the unit's ExecStart no longer passes {flag}"


def test_the_timer_follows_the_gates_own_switch():
    """A ratchet running while the gate is off deploys to staging for a measurement nobody is
    collecting — and the stop half is what keeps the switch a switch."""
    tasks = _TASKS.read_text()
    assert "staging-backfill.timer" in tasks
    assert "gitops_deploy_staging_gate" in tasks
    assert "stopped" in tasks, (
        "the timer is enabled but never stopped, so turning the gate off leaves the ratchet "
        "running"
    )


def test_the_timer_is_not_wanted_by_the_deploy_tick():
    # The ratchet is hourly and independent. Coupling it to the 30-minute deployer would put a
    # staging deploy inside the tick's budget, which is the thing slice 3 deliberately bounded.
    assert "gitops-deploy.service" not in _TIMER.read_text()
