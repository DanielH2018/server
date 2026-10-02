"""The registry pull self-test must not wait on a Job a single-node cluster cannot schedule.

`selftest-pull-job.yaml.j2` renders two Jobs. The first proves the registry node's own
containerd resolves the mirror; the second proves an AGENT node's does, over the vxlan and
through the NetworkPolicy's flannel.1 ipBlock. The second is pinned to a node that exists only
on a multi-node cluster, so on the staging cluster (one node, docs/archive/staging-cluster.md Decision 3)
it is unschedulable and the role's `kubectl wait` sits there until its 180s timeout.

That is a check that can neither pass nor usefully fail — the shape this repo warns about,
arriving as a stalled deploy rather than a green tile.

Two things have to agree and cannot see each other: the template's `{% if %}` and the wait
task's job list. These tests pin both to the same variable, and the last one pins them to each
other, because a manifest that stops rendering the Job while the wait still names it turns a
correct deploy into a NotFound failure.

The renders go through `_k8s_render.render_role_template`, the shared harness, so the context
under the gate is the role's real one rather than a four-key stand-in written here (#3202). One
read of the template's SOURCE survives, and `TEMPLATE_SOURCE_READERS` in
`ansible/tests/repo/test_guard_tests_read_renders_not_templates.py` carries its reason: a render
always has `k3s_agent_node_ips` defined, so no render can show what happens on a host that does
not define it.
"""

from lib import yaml_fast

from _helpers import ROLES, task_named
from _k8s_render import render_role_template

REGISTRY = ROLES / "k8s" / "registry"
PULL_TEMPLATE = REGISTRY / "templates" / "selftest-pull-job.yaml.j2"
REGISTRY_TASKS = REGISTRY / "tasks" / "main.yml"

AGENT_JOB = "registry-selftest-pull-agent"
LOCAL_JOB = "registry-selftest-pull"
AGENT_VAR = "k3s_agent_node_ips"
TEMPLATE = "selftest-pull-job.yaml.j2"


def _job_names(agent_node_ips: list[str]) -> set[str]:
    rendered = render_role_template("registry", TEMPLATE, {AGENT_VAR: agent_node_ips})
    docs = [d for d in yaml_fast.safe_load_all(rendered) if d]
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

    This is the one claim here a render cannot make: every render context defines the variable,
    so no render shows the undefined case. The gate's `| default([])` filter is read from the
    template's source instead, which is why this module is listed in TEMPLATE_SOURCE_READERS.
    """
    gates = [
        line.strip()
        for line in PULL_TEMPLATE.read_text().splitlines()
        if AGENT_VAR in line and line.lstrip().startswith("{%")
    ]
    assert gates, (
        f"no Jinja gate in {PULL_TEMPLATE} reads {AGENT_VAR}; the agent Job is unconditional "
        f"and a single-node cluster waits out its full 180s timeout"
    )
    for gate in gates:
        assert "default(" in gate, (
            f"the gate {gate!r} needs `| default([])`; without it this is an "
            f"undefined-variable error on every host whose inventory does not name "
            f"{AGENT_VAR}"
        )


def test_the_wait_task_and_the_template_gate_on_the_same_thing():
    """A manifest that stops rendering the Job while the wait names it fails NotFound."""
    wait = task_named(
        yaml_fast.safe_load(REGISTRY_TASKS.read_text()), "Wait for the pull self-tests"
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
