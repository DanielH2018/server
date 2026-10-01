"""Guards observability's hand-off to the end-of-play stabilisation gate.

observability is the one k8s role that rolls its own workloads. It sets `manifests_rollout: ''`,
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

  * **The lists drift.** The role spells its workloads out twice — the wait loop, and
    `observability_stabilise_workloads` in defaults, which the restart loop and the stabilisation
    tasks read. Add a seventh workload to the wait loop and forget the variable and the gate
    watches six of seven, reporting green for the one that crashloops.
  * **The inline wait gets queued into the drain.** It looks like the obvious next speedup —
    it is 54s of serial waiting that `k8s/manifests/tasks/drain.yml` would collapse to a max(). It cannot
    move: "Sync the live Grafana admin password" `kubectl exec`s into `deploy/grafana`, and the
    drain does not run until the end of the batch. Same shape as the two roles guarded in
    test_inline_rollout_gates.py, but keyed on a templated loop rather than a literal target.
  * **The snapshot drifts ahead of the wait.** Restart counts read while the old pod is still
    terminating are the OUTGOING pod's, and its replacement starts at zero — so the gate's
    `after <= before` comparison passes no matter what happened.
"""

from _helpers import REPO as _REPO
from _helpers import load_tasks, load_defaults
from _helpers import command_of as _cmd
from _helpers import render_expr


_ROLE = _REPO / "ansible/roles/k8s/observability"
_TASKS = _ROLE / "tasks" / "main.yml"
_MANIFESTS = _REPO / "ansible/roles/k8s/manifests"


def _index_of(predicate) -> int:
    for index, task in enumerate(load_tasks(_TASKS)):
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
    for task in load_tasks(_TASKS):
        pairs = _pairs(task.get("loop"))
        # Both workload loops carry otel-collector; nothing else in the role loops over
        # {kind, name} pairs, so this identifies them without matching on task names.
        if ("daemonset", "otel-collector") in pairs:
            found.append(pairs)
    return found


def test_the_role_lists_its_workloads_the_same_way_everywhere() -> None:
    declared = _pairs(load_defaults(_ROLE).get("observability_stabilise_workloads"))
    assert declared, (
        "observability_stabilise_workloads is missing from the role's defaults. The stabilisation "
        "snapshot iterates it; without it the play gate watches nothing for this role."
    )

    # Deliberately not a count. The role spells the workloads out once today (the wait loop;
    # the restart loop was pointed at observability_stabilise_workloads in #2858), and pointing
    # the last one at the variable too is a correct consolidation — a test that pinned the
    # number would fail for that improvement. What must hold is that every literal copy still
    # standing agrees with the declared list.
    for loop in _literal_workload_loops():
        assert loop == declared, (
            "observability rolls a different set of workloads than it hands to the stabilisation "
            f"gate. Rolled: {sorted(loop)}. Declared in observability_stabilise_workloads: "
            f"{sorted(declared)}. The gate would silently watch fewer workloads than rolled."
        )


def test_the_role_still_waits_inline_before_exec_ing_into_grafana() -> None:
    wait = _index_of(lambda t: "rollout status" in _cmd(t))
    exec_grafana = _index_of(lambda t: "exec deploy/grafana" in _cmd(t))

    assert wait >= 0, (
        "observability no longer waits on its own rollout. It cannot be queued into "
        "k8s/manifests/tasks/drain.yml: the drain runs at the end of the batch, and the Grafana admin "
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
        "observability no longer snapshots restart counts, so post_tasks/k8s_stabilise_gate.yml "
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
        for task in load_tasks(_TASKS)
        if "k8s_stabilise_watch" in str(task.get("ansible.builtin.set_fact", ""))
    ]
    assert appends, (
        "observability does not append to k8s_stabilise_watch. Its six workloads are the only "
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


def _snapshot_when() -> list[str]:
    """The `when:` of the restart-count snapshot — the task that feeds the gate."""
    snapshot = next(task for task in load_tasks(_TASKS) if "restartCount" in _cmd(task))
    when = snapshot["when"]
    assert isinstance(when, list), when
    return [str(condition) for condition in when]


def _snapshot_runs(**over) -> bool:
    """Evaluate the snapshot's `when:` the way Ansible does, against fake registers."""
    ctx = dict(
        k8s_no_mutate=False,
        manifests_render={"changed": True},
        manifests_secret_render={"changed": False},
        manifests_apply={
            "changed": True,
            "stdout": "deployment.apps/grafana configured",
        },
    )
    ctx.update(over)
    return all(
        bool(render_expr("{{ " + condition + " }}", **ctx))
        for condition in _snapshot_when()
    )


def test_an_inert_manifest_edit_hands_nothing_to_the_stabilisation_gate() -> None:
    """A YAML-comment or whitespace edit moves rendered bytes and no live object.

    The apply prints every object `unchanged`, so no `checksum/config` annotation moved and
    nothing restarted — snapshotting anyway appended all six workloads to `k8s_stabilise_watch`
    and bought the play gate's pause plus twelve kubectl reads for a deploy in which no pod
    moved (#3133, the same shape as #3125 in `k8s/manifests`). Evaluated rather than
    string-matched: `manifests_render is changed` is a substring of the conjunction that fixes
    this, so a textual assert would stay green either way.
    """
    inert = {"changed": False, "stdout": "deployment.apps/grafana unchanged"}

    assert _snapshot_runs(manifests_apply=inert) is False
    # RED-proof on the other side: the same render with an apply that moved a live object.
    assert _snapshot_runs() is True
    # A Secret edit still buys the soak — it restarts prometheus and grafana out of band, and
    # verify_secret_keys can patch a key after an apply that printed `unchanged`.
    assert (
        _snapshot_runs(manifests_apply=inert, manifests_secret_render={"changed": True})
        is True
    )
    # A dry run reads nothing live, so it hands the gate nothing whatever changed.
    assert _snapshot_runs(k8s_no_mutate=True) is False
