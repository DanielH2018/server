"""Guards claude-otel's hand-off to the end-of-play stabilisation gate.

claude-otel is the one k8s role that rolls its own workloads. It sets `manifests_rollout: ''`,
so the shared `k8s/manifests` role neither waits nor queues anything, and the role restarts and
waits on six workloads of its own in a different namespace.

Until 2026-08-22 it soaked them by importing `k8s/manifests`'s `assert_stable.yml`, which
carried a second 60s `pause` on top of the play-level one in
`post_tasks/k8s_stabilise_gate.yml`. Measured on a full deploy (pid 2004861) that pause was
60.02s of the role's 69.0s — the second-most expensive task in a 20-minute run, spent
duplicating a window the play already pays once. The role now snapshots restart counts itself
and appends to `k8s_stabilise_watch`, so the play gate soaks these six alongside everything
else.

Three things can silently break that, and none of them fails a deploy:

  * **The lists drift.** The role spells its workloads out three times — the restart loop, the
    wait loop, and `claude_otel_stabilise_workloads` in defaults. Add a seventh workload to the
    first two and forget the third and the gate watches six of seven, reporting green for the
    one that crashloops.
  * **The inline wait gets queued into the drain.** It looks like the obvious next speedup —
    it is 54s of serial waiting that `k8s/rollout-drain` would collapse to a max(). It cannot
    move: "Sync the live Grafana admin password" `kubectl exec`s into `deploy/grafana`, and the
    drain does not run until the end of the batch. Same shape as the two roles guarded in
    test_inline_rollout_gates.py, but keyed on a templated loop rather than a literal target.
  * **The snapshot drifts ahead of the wait.** Restart counts read while the old pod is still
    terminating are the OUTGOING pod's, and its replacement starts at zero — so the gate's
    `after <= before` comparison passes no matter what happened.
"""

from __future__ import annotations


import yaml
from _helpers import REPO as _REPO


_ROLE = _REPO / "ansible/roles/k8s/claude-otel"
_MANIFESTS = _REPO / "ansible/roles/k8s/manifests"


def _tasks() -> list[dict]:
    return yaml.safe_load((_ROLE / "tasks" / "main.yml").read_text()) or []


def _defaults() -> dict:
    return yaml.safe_load((_ROLE / "defaults" / "main.yml").read_text()) or {}


def _cmd(task: dict) -> str:
    module = task.get("ansible.builtin.command")
    if isinstance(module, dict):
        return str(module.get("cmd", ""))
    if isinstance(module, str):
        return module
    return ""


def _index_of(predicate) -> int:
    for index, task in enumerate(_tasks()):
        if predicate(task):
            return index
    return -1


def _pairs(loop: object) -> set[tuple[str, str]]:
    """The {kind, name} set of a literal loop, ignoring templated ones."""
    if not isinstance(loop, list):
        return set()
    return {
        (str(item.get("kind")), str(item.get("name")))
        for item in loop
        if isinstance(item, dict) and "name" in item
    }


def _literal_workload_loops() -> list[set[tuple[str, str]]]:
    found = []
    for task in _tasks():
        pairs = _pairs(task.get("loop"))
        # Both workload loops carry otel-collector; nothing else in the role loops over
        # {kind, name} pairs, so this identifies them without matching on task names.
        if ("daemonset", "otel-collector") in pairs:
            found.append(pairs)
    return found


def test_the_role_lists_its_workloads_the_same_way_everywhere() -> None:
    declared = _pairs(_defaults().get("claude_otel_stabilise_workloads"))
    assert declared, (
        "claude_otel_stabilise_workloads is missing from the role's defaults. The stabilisation "
        "snapshot iterates it; without it the play gate watches nothing for this role."
    )

    # Deliberately not a count. The role spells the workloads out twice today (the restart loop
    # and the wait loop), but pointing either at claude_otel_stabilise_workloads is a correct
    # consolidation — a test that pinned the number would fail for that improvement. What must
    # hold is that every literal copy still standing agrees with the declared list.
    for loop in _literal_workload_loops():
        assert loop == declared, (
            "claude-otel rolls a different set of workloads than it hands to the stabilisation "
            f"gate. Rolled: {sorted(loop)}. Declared in claude_otel_stabilise_workloads: "
            f"{sorted(declared)}. The gate would silently watch fewer workloads than rolled."
        )


def test_the_role_still_waits_inline_before_exec_ing_into_grafana() -> None:
    wait = _index_of(lambda t: "rollout status" in _cmd(t))
    exec_grafana = _index_of(lambda t: "exec deploy/grafana" in _cmd(t))

    assert wait >= 0, (
        "claude-otel no longer waits on its own rollout. It cannot be queued into "
        "k8s/rollout-drain: the drain runs at the end of the batch, and the Grafana admin "
        "password sync execs into deploy/grafana before then."
    )
    assert exec_grafana >= 0, (
        "nothing execs into deploy/grafana any more — re-check whether the inline wait is "
        "still needed, and delete this guard with it if not."
    )
    assert wait < exec_grafana, (
        f"the rollout wait runs at task {wait}, after the Grafana exec at task {exec_grafana}. "
        "The exec would hit a pod that is still terminating."
    )


def test_the_restart_snapshot_is_taken_after_the_wait() -> None:
    wait = _index_of(lambda t: "rollout status" in _cmd(t))
    snapshot = _index_of(lambda t: "restartCount" in _cmd(t))

    assert snapshot >= 0, (
        "claude-otel no longer snapshots restart counts, so post_tasks/k8s_stabilise_gate.yml "
        "has no `restarts_before` to compare against for these six workloads."
    )
    assert wait < snapshot, (
        f"restart counts are snapshotted at task {snapshot}, before the rollout wait at task "
        f"{wait}. That reads the OUTGOING pod's counts; the replacement starts at zero, so the "
        "gate's `after <= before` comparison passes no matter what happened."
    )


def test_the_role_feeds_the_play_level_gate() -> None:
    appends = [
        task
        for task in _tasks()
        if "k8s_stabilise_watch" in str(task.get("ansible.builtin.set_fact", ""))
    ]
    assert appends, (
        "claude-otel does not append to k8s_stabilise_watch. Its six workloads are the only "
        "ones outside k8s_namespace and nothing else queues them, so the play gate would skip "
        "them entirely — the 2026-08-07 kube-state-metrics crashloop goes unseen again."
    )


def test_assert_stable_is_gone() -> None:
    # It duplicated post_tasks/k8s_stabilise_gate.yml, pause and all. Re-adding it is how the
    # 60s comes back.
    assert not (_MANIFESTS / "tasks" / "assert_stable.yml").exists(), (
        "roles/k8s/manifests/tasks/assert_stable.yml is back. It carries its own 60s pause on "
        "top of the play-level window; use post_tasks/k8s_stabilise_gate.yml instead."
    )

    importers = [
        path
        for path in (_REPO / "ansible/roles").rglob("*.yml")
        if "tasks_from: assert_stable" in path.read_text()
    ]
    assert not importers, (
        f"{[str(p) for p in importers]} import assert_stable, which no longer exists."
    )
