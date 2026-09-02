"""The no-pods assert in `k8s/rollout-drain` must tell a label typo from a deliberate zero.

A workload scaled to zero has no pods on purpose. Before 2026-09-02 the assert read only the
restart snapshot, so an empty snapshot was indistinguishable from a mislabelled workload and it
failed the whole play — which is what `terraria` at `replicas: 0` did on its first deploy.

The exemption must stay narrow. It is allowed to pass a workload the CLUSTER agrees wants zero
pods, and nothing else: a mislabelled workload that is actually running still reports a non-zero
desired count, so it still fails. Both halves are asserted below, because a check that fires on
everything and one that fires on nothing look identical from the passing side.
"""

from __future__ import annotations

import yaml
from _helpers import REPO, render_expr

DRAIN = REPO / "ansible/roles/k8s/rollout-drain/tasks/main.yml"


def _no_pods_assert() -> dict:
    """The `Fail if a pod selector matched no pods` task, as parsed YAML."""
    tasks = yaml.safe_load(DRAIN.read_text())
    named = [
        t for t in tasks if t.get("name") == "Fail if a pod selector matched no pods"
    ]
    assert len(named) == 1, f"expected exactly one no-pods assert, found {len(named)}"
    return named[0]


def _fires(pods_stdout: str, desired_stdout: str) -> bool:
    """True when the assert PASSES for a workload with these two command outputs.

    `pods_stdout` is the restart-count jsonpath (empty when the selector matched nothing);
    `desired_stdout` is the desired-pod-count jsonpath.
    """
    expression = _no_pods_assert()["ansible.builtin.assert"]["that"]
    item = ({"stdout": pods_stdout}, {"stdout": desired_stdout})
    rendered = render_expr("{{ " + expression + " }}", item=item)
    # The repo's Jinja env is a native one, so a boolean expression comes back as a real bool.
    # Accept the string form too rather than depending on which env a future helper hands over.
    if isinstance(rendered, str):
        return rendered.strip() == "True"
    return bool(rendered)


def test_a_workload_deliberately_at_zero_is_accepted() -> None:
    """The ACCEPT half: no pods and a desired count of zero is the scale-to-zero case."""
    assert _fires(pods_stdout="", desired_stdout="0"), (
        "a workload at replicas: 0 has no pods by design and must not fail the drain gate"
    )


def test_a_mislabelled_running_workload_is_still_rejected() -> None:
    """The REJECT half: no pods while the cluster wants some is the typo the gate exists for."""
    assert not _fires(pods_stdout="", desired_stdout="1"), (
        "a workload whose selector matched nothing while the cluster wants pods must still fail"
    )


def test_a_daemonset_with_no_scheduled_nodes_is_accepted() -> None:
    """A DaemonSet reports `desiredNumberScheduled`, and zero means no node matches it.

    Same shape as the Deployment case and reached by the same expression, because the task
    concatenates both jsonpaths and only one of them ever renders.
    """
    assert _fires(pods_stdout="", desired_stdout="0")


def test_a_running_workload_is_accepted_whatever_the_desired_count() -> None:
    """The ordinary path stays untouched: pods matched, so the desired count is not consulted."""
    assert _fires(pods_stdout="0 0", desired_stdout="2")


def test_the_assert_reads_the_desired_count_task() -> None:
    """The exemption is only sound if the desired count comes from the cluster, not from vars.

    A future edit that satisfied the expression from `k8s_pending_rollouts` instead would let a
    role's own declaration excuse it from the gate. Pin the read to a `kubectl get` on the
    workload itself.
    """
    tasks = yaml.safe_load(DRAIN.read_text())
    named = [
        t
        for t in tasks
        if t.get("name") == "Read the desired pod count for the workloads that changed"
    ]
    assert len(named) == 1, "the desired-count read must exist exactly once"
    cmd = named[0]["ansible.builtin.command"]["cmd"]
    assert "kubectl" in cmd and "get" in cmd, cmd
    assert ".spec.replicas" in cmd, "Deployments report their desired count here"
    assert ".status.desiredNumberScheduled" in cmd, "DaemonSets report theirs here"
