"""The registry pull self-test must not wait on a Job a single-node cluster cannot schedule.

`selftest-pull-job.yaml.j2` renders two Jobs. The first proves the registry node's own
containerd resolves the mirror; the second proves an AGENT node's does, over the vxlan and
through the NetworkPolicy's flannel.1 ipBlock. The second is pinned to a node that exists only
on a multi-node cluster, so on the staging cluster (one node, docs/staging-cluster.md Decision 3)
it is unschedulable and the role's `kubectl wait` sits there until its 180s timeout.

That is a check that can neither pass nor usefully fail — the shape this repo warns about,
arriving as a stalled deploy rather than a green tile.

Two things have to agree and cannot see each other: the template's `{% if %}` and the wait
task's job list. These tests pin both to the same variable, and the last one pins them to each
other, because a manifest that stops rendering the Job while the wait still names it turns a
correct deploy into a NotFound failure.
"""

from __future__ import annotations

import yaml

from _helpers import ROLES, jinja_env, task_named

REGISTRY = ROLES / "k8s" / "registry"
PULL_TEMPLATE = REGISTRY / "templates" / "selftest-pull-job.yaml.j2"
REGISTRY_TASKS = REGISTRY / "tasks" / "main.yml"

AGENT_JOB = "registry-selftest-pull-agent"
LOCAL_JOB = "registry-selftest-pull"
AGENT_VAR = "k3s_agent_node_ips"

_BASE_CONTEXT = {
    "k8s_namespace": "homelab",
    "k8s_registry_node": "daniel-box",
    "k8s_registry_pull_host": "registry.local:5000",
    "registry_k8s_probe_repo": "selftest",
}


def _job_names(agent_node_ips: list[str]) -> set[str]:
    context = {**_BASE_CONTEXT, AGENT_VAR: agent_node_ips}
    rendered = jinja_env().from_string(PULL_TEMPLATE.read_text()).render(context)
    docs = [d for d in yaml.safe_load_all(rendered) if d]
    assert docs, (
        f"{PULL_TEMPLATE} rendered no YAML documents with {AGENT_VAR}={agent_node_ips!r}. "
        f"The local pull Job is unconditional, so an empty render means the template broke, "
        f"not that the gate fired."
    )
    return {d["metadata"]["name"] for d in docs}


def test_a_multi_node_cluster_still_proves_the_agent_pull_path():
    """The regression that matters most: silently dropping prod's only cross-node proof."""
    names = _job_names(["10.0.0.161"])
    assert names == {LOCAL_JOB, AGENT_JOB}, (
        f"{PULL_TEMPLATE} rendered {sorted(names)} for a cluster WITH an agent node. Prod must "
        f"keep both halves — the agent Job is the one that fails loudly when the "
        f"NetworkPolicy's flannel.1 ipBlock SNAT assumption is wrong, and nothing else covers it."
    )


def test_a_single_node_cluster_omits_the_job_it_cannot_schedule():
    names = _job_names([])
    assert names == {LOCAL_JOB}, (
        f"{PULL_TEMPLATE} rendered {sorted(names)} for a single-node cluster. {AGENT_JOB} is "
        f"pinned to a second node that does not exist there, so it stays Pending and the "
        f"role's wait blocks for its full 180s timeout before failing."
    )


def test_an_undefined_agent_list_is_treated_as_no_agents():
    """`k3s_agent_node_ips` is host_vars on daniel-box and a role default elsewhere.

    The registry role is not the k3s role, so it does not inherit that default. Rendering for a
    host that never declares the variable must fail closed to "no agents" rather than raising.
    """
    context = dict(_BASE_CONTEXT)
    rendered = jinja_env().from_string(PULL_TEMPLATE.read_text()).render(context)
    names = {d["metadata"]["name"] for d in yaml.safe_load_all(rendered) if d}
    assert names == {LOCAL_JOB}, (
        f"{PULL_TEMPLATE} rendered {sorted(names)} with {AGENT_VAR} undefined. The gate needs "
        f"`| default([])`; without it this is an undefined-variable error on every host whose "
        f"inventory does not name the variable."
    )


def test_the_wait_task_and_the_template_gate_on_the_same_thing():
    """A manifest that stops rendering the Job while the wait names it fails NotFound."""
    wait = task_named(
        yaml.safe_load(REGISTRY_TASKS.read_text()), "Wait for the pull self-tests"
    )
    gate = str(wait.get("vars", {}).get("registry_selftest_pull_jobs", ""))
    assert AGENT_VAR in gate, (
        f"the pull-wait task in {REGISTRY_TASKS} does not condition its job list on "
        f"{AGENT_VAR}. It reads {gate!r}. The template gates on that variable, so an "
        f"unconditional wait names a Job that was never applied and the deploy fails NotFound "
        f"on the one cluster this change exists to support."
    )
    assert AGENT_JOB in gate and LOCAL_JOB in gate, (
        f"the pull-wait task in {REGISTRY_TASKS} waits on {gate!r}, which does not name both "
        f"halves. Dropping the local half would leave the pull path unproven everywhere."
    )
