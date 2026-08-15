"""Guards on the deferred k8s rollout gate.

`roles/k8s/manifests` used to wait on its own rollout inline and then run `assert_stable.yml`
inline too. Both are now hoisted out: the role QUEUES into `k8s_pending_rollouts`,
`roles/k8s/rollout-drain` waits for a whole batch at once, and one stabilisation window runs in
`deploy.yml`'s post_tasks for the entire play.

That refactor moved a crashloop gate away from the role it protects, which makes it quietly
droppable. Every failure mode below is a FALSE GREEN — the deploy reports success and the gate
simply never ran:

  * `manifests` waits inline again -> batching silently reverts to serial (no failure, just the
    59-minute deploy back);
  * `manifests` stops queueing -> nothing is ever waited on OR soaked;
  * `deploy.yml` loses the post_tasks gate -> rollouts are waited on but never soaked, which is
    exactly the kube-state-metrics failure of 2026-08-07 (clean logs, ok=41 failed=0, pod
    crashlooping on a liveness 404);
  * `rollout-drain` stops clearing the queue -> later batches re-wait earlier workloads, and the
    restart snapshot is taken twice for the same service;
  * `configarr` loses its drain -> its reconcile Job fires into sonarr's ~5-minute startup and
    reconciles against a dead API.
"""

from __future__ import annotations

from pathlib import Path

import yaml

_REPO = Path(__file__).resolve().parents[2]
_MANIFESTS = _REPO / "ansible/roles/k8s/manifests/tasks/main.yml"
_DRAIN = _REPO / "ansible/roles/k8s/rollout-drain/tasks/main.yml"
_CONFIGARR = _REPO / "ansible/roles/k8s/configarr/tasks/main.yml"
_DEPLOY = _REPO / "ansible/deploy.yml"
_BATCH = _REPO / "ansible/tasks/k8s_batch.yml"
_ALL_VARS = _REPO / "ansible/inventory/group_vars/all.yml"


def _tasks(path: Path) -> list[dict]:
    return yaml.safe_load(path.read_text()) or []


def _plays() -> list[dict]:
    return yaml.safe_load(_DEPLOY.read_text()) or []


def _k8s_play() -> dict:
    for play in _plays():
        if play.get("name") == "Deploy k8s workloads":
            return play
    raise AssertionError("deploy.yml no longer has a 'Deploy k8s workloads' play")


def _cmd(task: dict) -> str:
    """The command string a task runs, whichever module shape it uses."""
    for key in ("ansible.builtin.command", "ansible.builtin.shell"):
        mod = task.get(key)
        if isinstance(mod, dict):
            return str(mod.get("cmd", ""))
        if isinstance(mod, str):
            return mod
    return ""


def test_manifests_queues_the_rollout_instead_of_waiting() -> None:
    tasks = _tasks(_MANIFESTS)
    queued = [
        t
        for t in tasks
        if "k8s_pending_rollouts" in str(t.get("ansible.builtin.set_fact", ""))
    ]
    assert queued, (
        "roles/k8s/manifests no longer queues into k8s_pending_rollouts, so nothing waits on its "
        "rollout and nothing soaks it. See the module docstring."
    )


def test_manifests_does_not_wait_on_its_own_rollout() -> None:
    inline = [t for t in _tasks(_MANIFESTS) if "rollout status" in _cmd(t)]
    assert not inline, (
        "roles/k8s/manifests waits on `rollout status` inline again. That serialises the whole "
        "play — 1386s across 31 services measured 2026-08-15 — and makes "
        "k8s_rollout_batch_width a no-op. The wait belongs in roles/k8s/rollout-drain."
    )


def test_manifests_records_whether_the_render_changed() -> None:
    """`changed` decides whether the stabilisation gate watches the workload at all."""
    queued = next(
        str(t["ansible.builtin.set_fact"])
        for t in _tasks(_MANIFESTS)
        if "k8s_pending_rollouts" in str(t.get("ansible.builtin.set_fact", ""))
    )
    assert "manifests_render is changed" in queued
    assert "manifests_secret_render is changed" in queued


def test_drain_waits_then_clears_the_queue() -> None:
    tasks = _tasks(_DRAIN)
    assert any("rollout status" in _cmd(t) for t in tasks), (
        "roles/k8s/rollout-drain no longer runs `rollout status`, so queued rollouts are never "
        "waited on."
    )
    cleared = [
        t
        for t in tasks
        if t.get("ansible.builtin.set_fact", {}).get("k8s_pending_rollouts") == []
    ]
    assert cleared, (
        "roles/k8s/rollout-drain must reset k8s_pending_rollouts to [], or every later batch "
        "re-waits the earlier ones and snapshots their restart counts twice."
    )


def test_drain_does_not_use_kubectl_wait_for_available() -> None:
    """`--for=condition=Available` returns before the new pod is scheduled on this cluster.

    Every Deployment in the namespace is single-replica; the RollingUpdate ones have
    maxUnavailable 25% -> 0, so the OLD pod satisfies Available for the whole rolling update.
    """
    body = _DRAIN.read_text()
    offending = [
        line
        for line in body.splitlines()
        if "condition=Available" in line and not line.strip().startswith("#")
    ]
    assert not offending, (
        "roles/k8s/rollout-drain uses `kubectl wait --for=condition=Available`, which does not "
        "gate on the new ReplicaSet here and reports success on a rollout that never started. "
        "Use `kubectl rollout status`."
    )


def test_deploy_runs_the_stabilisation_gate_in_post_tasks() -> None:
    play = _k8s_play()
    included = [
        t.get("ansible.builtin.include_tasks", "") for t in play.get("post_tasks", [])
    ]
    assert any("k8s_stabilise_gate" in str(i) for i in included), (
        "the k8s play lost its post_tasks stabilisation gate. Rollouts would still be waited on, "
        "but a workload that passes readiness and then crashloops on a bad liveness probe would "
        "deploy green — the 2026-08-07 kube-state-metrics failure."
    )


def test_deploy_batches_the_k8s_roles() -> None:
    play = _k8s_play()
    batched = [
        t
        for t in play.get("tasks", [])
        if "k8s_batch" in str(t.get("ansible.builtin.include_tasks", ""))
    ]
    assert batched, "the k8s play no longer includes tasks/k8s_batch.yml"
    loop = str(batched[0].get("loop", ""))
    assert "batch(" in loop and "k8s_rollout_batch_width" in loop, (
        "the k8s play must chunk containers_list by k8s_rollout_batch_width — that width is the "
        "only bound on how many workloads roll at once now that the inline wait is gone."
    )


def test_batch_drains_after_applying() -> None:
    """Applying without draining would let every batch pile onto the previous one's rollouts."""
    tasks = _tasks(_BATCH)
    roles = [str(t.get("ansible.builtin.include_role", "")) for t in tasks]
    assert any("k8s/rollout-drain" in r for r in roles)
    assert "k8s/rollout-drain" in roles[-1], (
        "the drain must be the LAST task in a batch, after every role in it has applied — "
        "draining earlier waits on a partially-queued batch."
    )


def test_batch_width_is_declared_and_conservative() -> None:
    width = yaml.safe_load(_ALL_VARS.read_text())["k8s_rollout_batch_width"]
    assert isinstance(width, int) and width >= 1
    assert width <= 10, (
        f"k8s_rollout_batch_width={width} rolls that many workloads at once. 37 of the 50 "
        "Deployments here use strategy Recreate, so a wide batch means that many simultaneous "
        "Longhorn detach/attach cycles and availability gaps, not extra surge replicas. Widen "
        "only against a measured run."
    )


def test_configarr_drains_before_reconciling() -> None:
    """configarr reconciles against sonarr/radarr over HTTP and is listed after them."""
    tasks = _tasks(_CONFIGARR)
    drain_at = next(
        (
            i
            for i, t in enumerate(tasks)
            if "k8s/rollout-drain" in str(t.get("ansible.builtin.include_role", ""))
        ),
        None,
    )
    assert drain_at is not None, (
        "configarr no longer drains pending rollouts before reconciling. Its Job would fire into "
        "sonarr's ~5-minute startup and reconcile against a dead API."
    )
    job_at = next(
        i for i, t in enumerate(tasks) if "create job configarr-deploy" in _cmd(t)
    )
    assert drain_at < job_at, (
        "configarr's rollout drain must come BEFORE it creates the reconcile Job."
    )
