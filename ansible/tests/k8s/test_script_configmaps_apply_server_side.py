"""Every script ConfigMap in the k8s roles is applied server-side.

Client-side `kubectl apply` stores the whole object a second time in the
`last-applied-configuration` annotation, and annotations are capped at 262144 bytes.
monitor-bridge's runtime modules total ~255 KB of Python; JSON-escaped into that annotation
they cross the cap, and the apply is refused with `metadata.annotations: Too long` while the
pod keeps running the previous code. Server-side
apply writes no such annotation, so the cap does not apply.

The rule covers every role with an `Apply the script ConfigMap` task in any of its task files, not just the one that
hit the cap: the other three ship one small script each today, and a guard scoped to the
instance that failed is the guard-scope shape this repo has paid for before. The roles are
derived from the tree, so a fifth script ConfigMap joins the rule the day it appears.

No role's own suite can see this: pytest imports the modules from files/ and never renders or
applies the ConfigMap. A companion assertion pins the reason for monitor-bridge — if its
modules ever shrink well below the cap the comment in its tasks/main.yml is the thing to
revisit, not this guard.

Run: uv run pytest ansible/tests/k8s/test_script_configmaps_apply_server_side.py
"""

import pytest
from lib import yaml_fast
from _helpers import K8S_ROLES
from _role_census import role_dirs, role_task_files

TASK_NAME = "Apply the script ConfigMap"
ANNOTATION_CAP = 262144


def _apply_tasks(tasks):
    """The apply tasks in one task file, including game-stats' per-game `... for <game>`."""
    return [
        t
        for t in tasks or []
        if isinstance(t, dict)
        and (
            t.get("name") == TASK_NAME
            or str(t.get("name")).startswith(f"{TASK_NAME} for ")
        )
    ]


def _role_apply_tasks(role):
    """Every apply task across the role's task files: game-stats keeps one per game file."""
    return [
        task
        for tasks_file in role_task_files(role)
        for task in _apply_tasks(yaml_fast.safe_load(tasks_file.read_text()))
    ]


def _apply_cmd(task):
    module = task.get("ansible.builtin.command") or task.get("command")
    assert module, f"{TASK_NAME!r} is not an ansible.builtin.command task"
    return " ".join(module["cmd"].split())


def _roles_with_script_configmaps():
    """Every k8s role whose task files carry the apply task — derived, not listed."""
    return [
        role.name
        for role in role_dirs(K8S_ROLES)
        if (role / "tasks").is_dir() and _role_apply_tasks(role)
    ]


ROLES = _roles_with_script_configmaps()


def test_the_derivation_finds_the_known_roles():
    # Without this the parametrized test below passes vacuously if the task is renamed.
    assert {
        "monitor-bridge",
        "autofix-bridge",
        "game-stats",
    } <= set(ROLES), ROLES


@pytest.mark.parametrize("role", ROLES)
def test_the_script_configmap_is_applied_server_side(role):
    matches = _role_apply_tasks(K8S_ROLES / role)
    assert matches, f"{role}: expected a {TASK_NAME!r} task, found none"
    for task in matches:
        cmd = _apply_cmd(task)
        assert "--server-side" in cmd, (
            f"{role}: {task['name']!r} runs `{cmd}` — client-side apply re-stores the object "
            "in an annotation capped at 262144 bytes"
        )
        assert "--force-conflicts" in cmd, (
            f"{role}: the data keys were owned by the client-side field manager before the "
            "switch; without --force-conflicts the first server-side apply is rejected"
        )


def test_monitor_bridge_still_needs_it():
    """The bridge's modules are large enough that the client-side form would be refused."""
    # A JSON-escaped copy of the source lands in the annotation, so the raw byte count is a
    # lower bound on what client-side apply would store. Pin that it is within a factor of the
    # cap — if this ever fails, the source shrank and the comment in tasks/main.yml is stale.
    files = K8S_ROLES / "monitor-bridge" / "files"
    # `rglob`: the modules are packages under files/, and a one-level glob would sum check.py
    # alone.
    total = sum(
        p.stat().st_size
        for p in files.rglob("*.py")
        if "__pycache__" not in p.parts
        and not p.name.startswith("test_")
        and p.name != "conftest.py"
    )
    assert total > ANNOTATION_CAP // 2


def test_checker_rejects_a_client_side_apply():
    bad = [
        {
            "name": TASK_NAME,
            "ansible.builtin.command": {
                "cmd": "k3s kubectl apply -f /etc/rancher/k3s/x/configmap.yaml"
            },
        }
    ]
    assert len(_apply_tasks(bad)) == 1
    assert "--server-side" not in _apply_cmd(bad[0])
