"""The release record's rollout expectation, rendered the way Ansible renders it.

Shared by the two test modules that pin `release_stamp.yml`'s `rollouts[]` decision:
`k8s/test_release_stamp_rollout_expectation.py` (the shared restart, #1867 and #1988) and
`k8s/test_self_rollouts_follow_the_apply.py` (the two roles that roll their own workloads,
#1902 and #1994). The expression is rendered through Ansible's own filters and tests against
fake registers, one append per loop target, exactly as the play accumulates it.
"""

from _helpers import ANSIBLE, load_tasks, load_yaml, render_expr, task_named, walk_tasks

MANIFESTS_TASKS = ANSIBLE / "roles/k8s/manifests/tasks"
STAMP = load_tasks(MANIFESTS_TASKS / "release_stamp.yml")
MAIN = load_tasks(MANIFESTS_TASKS / "main.yml")
CLAUDE_OTEL = ANSIBLE / "roles/k8s/claude-otel"
PIHOLE = ANSIBLE / "roles/k8s/pihole"
EXPECT = task_named(STAMP, "Work out which workloads this apply must roll")
LOOP_EXPR = EXPECT["loop"]
FACT_EXPR = EXPECT["ansible.builtin.set_fact"]["manifests_release_rollouts"]

BASE = dict(
    manifests_service="prowlarr",
    manifests_rollout="prowlarr",
    manifests_rollout_kind="deploy",
    manifests_extra_rollouts=[
        {"name": "flaresolverr", "kind": "deploy", "image": "flaresolverr"}
    ],
    manifests_self_rollouts=[],
    manifests_render={"changed": True},
    manifests_secret_render={"changed": False},
    manifests_apply={"stdout": "deployment.apps/prowlarr configured"},
    k8s_rebuilt_images=[],
    manifests_rolled_by_apply={},
)


def rollouts(**over):
    """Run the loop + set_fact the way Ansible does: one append per target, accumulating."""
    ctx = {**BASE, **over}
    acc = []
    for target in render_expr(LOOP_EXPR, **ctx):
        acc = render_expr(
            FACT_EXPR,
            manifests_release_rollouts=acc,
            manifests_release_target=target,
            **ctx,
        )
    return {r["name"]: r for r in acc}


def include_role_vars(role_dir):
    """The `vars:` a role hands to `include_role: k8s/manifests`."""
    for task in walk_tasks(load_tasks(role_dir / "tasks/main.yml")):
        include = task.get("ansible.builtin.include_role") or task.get("include_role")
        if isinstance(include, dict) and include.get("name") == "k8s/manifests":
            return task["vars"]
    raise AssertionError(f"{role_dir.name} does not include k8s/manifests")


OBSERVABILITY_NAMESPACE = load_yaml(ANSIBLE / "inventory/group_vars/all.yml")[
    "k8s_observability_namespace"
]


def claude_otel_self_rollouts():
    """The declaration as the play sees it: a template over the role's defaults and the
    group var naming the namespace its workloads live in."""
    declared = include_role_vars(CLAUDE_OTEL)["manifests_self_rollouts"]
    defaults = load_yaml(CLAUDE_OTEL / "defaults/main.yml")
    return render_expr(
        declared, k8s_observability_namespace=OBSERVABILITY_NAMESPACE, **defaults
    )
