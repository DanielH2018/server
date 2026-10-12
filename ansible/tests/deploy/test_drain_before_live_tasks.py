"""netpol-baseline drains the batch's queued rollouts before it reads the cluster or applies.

`ansible/tasks/k8s_batch.yml` drains rollouts once, at the END of a batch. A role that runs
in the same batch as a workload therefore sees that workload mid-roll unless it drains first.
For netpol-baseline that means its live exempt-set reads, its policy applies and its probe's
readiness gate all run against a half-rolled cluster, and the probe fails on timing with the
fencing already applied. Its first task includes `k8s/manifests` with `tasks_from: drain.yml`,
the drain configarr runs before its reconcile, so the role's batch no longer matters.

The rule read here: every task ahead of the drain include is a pure check, one of
`_PURE_MODULES`, which neither applies nor reads the cluster. A role with no drain at all
breaks the rule too. `_DRAINS_FIRST` is the population, and netpol-baseline is its named
member, so a renamed role or task file fails rather than passing vacuously.

Run: uv run pytest ansible/tests/deploy/test_drain_before_live_tasks.py
"""

import pytest

from lib import yaml_fast
from _helpers import REPO

_K8S_ROLES = REPO / "ansible/roles/k8s"

# The roles that must drain before they touch the cluster. D4.1 of the target architecture
# widens this to every role it orders after all workloads.
_DRAINS_FIRST = frozenset({"netpol-baseline"})

# Modules that run on the controller and read nothing live, so one may precede the drain.
# Not `meta`: `meta: flush_handlers` runs notified handlers, which can apply anything.
_PURE_MODULES = frozenset(
    prefix + name
    for prefix in ("", "ansible.builtin.")
    for name in ("assert", "fail", "debug", "set_fact")
)

_INCLUDE_ROLE = ("ansible.builtin.include_role", "include_role")


def _is_drain(task: dict) -> bool:
    for key in _INCLUDE_ROLE:
        args = task.get(key)
        if isinstance(args, dict):
            return (
                args.get("name") == "k8s/manifests"
                and args.get("tasks_from") == "drain.yml"
            )
    return False


def _is_pure(task: dict) -> bool:
    return any(key in _PURE_MODULES for key in task)


def _drain_problem(tasks: list[dict]) -> str | None:
    """Why `tasks` fails the drain-first rule, or None when it holds.

    A task that is neither the drain nor a pure check counts as live. That includes a block or
    an include, whose contents this does not read, so the rule errs towards refusing.
    """
    for index, task in enumerate(tasks):
        if _is_drain(task):
            return None
        if not _is_pure(task):
            return (
                f"task {index} ({task.get('name', '<unnamed>')!r}) can apply or read live "
                "state, and it runs before any k8s/manifests drain.yml include"
            )
    return "no task includes k8s/manifests with tasks_from: drain.yml"


def _role_tasks(role: str) -> list[dict]:
    return yaml_fast.safe_load((_K8S_ROLES / role / "tasks/main.yml").read_text()) or []


def test_netpol_baseline_is_a_named_member_with_live_tasks():
    """Non-vacuity. A rename drops the member, and a role whose tasks are all pure would pass
    the rule below while checking nothing."""
    assert "netpol-baseline" in _DRAINS_FIRST
    tasks = _role_tasks("netpol-baseline")
    assert any(not _is_pure(t) and not _is_drain(t) for t in tasks)
    # The plan pins the drain as the FIRST task, not merely ahead of the live ones: a pure
    # module can still read the cluster through a lookup in its arguments.
    assert _is_drain(tasks[0])


@pytest.mark.parametrize("role", sorted(_DRAINS_FIRST))
def test_role_drains_before_any_live_task(role):
    problem = _drain_problem(_role_tasks(role))
    assert problem is None, (
        f"{role}: {problem}. Include k8s/manifests with tasks_from: drain.yml as the role's "
        "first task, so rollouts queued earlier in its batch finish before it runs."
    )


_APPLY = {"name": "Deploy", "ansible.builtin.include_role": {"name": "k8s/manifests"}}
_DRAIN = {
    "name": "Drain",
    "ansible.builtin.include_role": {
        "name": "k8s/manifests",
        "tasks_from": "drain.yml",
    },
}
_READ = {
    "name": "Read live",
    "ansible.builtin.command": {"cmd": "k3s kubectl get pods"},
}
_CHECK = {"name": "Check levers", "ansible.builtin.assert": {"that": ["true"]}}


@pytest.mark.parametrize(
    "tasks",
    [
        pytest.param([_APPLY, _DRAIN, _READ], id="drain-after-apply"),
        pytest.param([_READ, _DRAIN, _APPLY], id="drain-after-read"),
        pytest.param([_CHECK, _APPLY, _READ], id="no-drain"),
    ],
)
def test_a_drain_after_a_live_task_is_flagged(tasks):
    assert _drain_problem(tasks) is not None


@pytest.mark.parametrize(
    "tasks",
    [
        pytest.param([_DRAIN, _CHECK, _APPLY, _READ], id="drain-first"),
        pytest.param([_CHECK, _DRAIN, _APPLY, _READ], id="pure-check-then-drain"),
    ],
)
def test_a_drain_ahead_of_every_live_task_is_clean(tasks):
    assert _drain_problem(tasks) is None
