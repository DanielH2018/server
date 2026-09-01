"""Every script ConfigMap in the k8s roles is applied server-side.

Client-side `kubectl apply` stores the whole object a second time in the
`last-applied-configuration` annotation, and annotations are capped at 262144 bytes.
monitor-bridge's runtime modules total ~255 KB of Python; JSON-escaped into that annotation
they crossed the cap on 2026-09-01 (PR #725's deploy), and the apply was refused with
`metadata.annotations: Too long` while the pod kept running the previous code. Server-side
apply writes no such annotation, so the cap does not apply.

The rule covers every role with an `Apply the script ConfigMap` task, not just the one that
hit the cap: the other three ship one small script each today, and a guard scoped to the
instance that failed is the guard-scope shape this repo has paid for before. The roles are
derived from the tree, so a fifth script ConfigMap joins the rule the day it appears.

No role's own suite can see this: pytest imports the modules from files/ and never renders or
applies the ConfigMap. A companion assertion pins the reason for monitor-bridge — if its
modules ever shrink well below the cap the comment in its tasks/main.yml is the thing to
revisit, not this guard.

Run: uv run pytest ansible/tests/test_script_configmaps_apply_server_side.py
"""

import pytest
import yaml
from _helpers import REPO

K8S = REPO / "ansible" / "roles" / "k8s"
TASK_NAME = "Apply the script ConfigMap"
ANNOTATION_CAP = 262144


def _apply_tasks(tasks):
    return [t for t in tasks if isinstance(t, dict) and t.get("name") == TASK_NAME]


def _apply_cmd(task):
    module = task.get("ansible.builtin.command") or task.get("command")
    assert module, f"{TASK_NAME!r} is not an ansible.builtin.command task"
    return " ".join(module["cmd"].split())


def _roles_with_script_configmaps():
    """Every k8s role whose tasks/main.yml carries the apply task — derived, not listed."""
    found = []
    for role in sorted(K8S.iterdir()):
        tasks_file = role / "tasks" / "main.yml"
        if not tasks_file.is_file():
            continue
        tasks = yaml.safe_load(tasks_file.read_text()) or []
        if _apply_tasks(tasks):
            found.append(role.name)
    return found


ROLES = _roles_with_script_configmaps()


def test_the_derivation_finds_the_known_roles():
    # Without this the parametrized test below passes vacuously if the task is renamed.
    assert {
        "monitor-bridge",
        "autofix-bridge",
        "valheim-stats",
        "terraria-stats",
    } <= set(ROLES), ROLES


@pytest.mark.parametrize("role", ROLES)
def test_the_script_configmap_is_applied_server_side(role):
    tasks = yaml.safe_load((K8S / role / "tasks" / "main.yml").read_text())
    matches = _apply_tasks(tasks)
    assert len(matches) == 1, (
        f"{role}: expected one {TASK_NAME!r} task, found {len(matches)}"
    )
    cmd = _apply_cmd(matches[0])
    assert "--server-side" in cmd, (
        f"{role}: {TASK_NAME!r} runs `{cmd}` — client-side apply re-stores the object in an "
        "annotation capped at 262144 bytes"
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
    files = K8S / "monitor-bridge" / "files"
    total = sum(
        p.stat().st_size
        for p in files.glob("*.py")
        if not p.name.startswith("test_") and p.name != "conftest.py"
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
