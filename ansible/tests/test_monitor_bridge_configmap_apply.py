"""monitor-bridge's script ConfigMap must be applied server-side.

Client-side `kubectl apply` stores the whole object a second time in the
`last-applied-configuration` annotation, and annotations are capped at 262144 bytes. The
bridge's runtime modules total ~255 KB of Python; JSON-escaped into that annotation they
crossed the cap on 2026-09-01 (PR #725's deploy), and the apply was refused with
`metadata.annotations: Too long` while the pod kept running the previous code. Server-side
apply writes no such annotation, so the cap does not apply.

The role's own suite cannot see this: pytest imports the modules from files/ and never
renders or applies the ConfigMap. This test pins the apply form, and a companion assertion
pins the reason — if the modules ever shrink well below the cap the comment in tasks/main.yml
is the thing to revisit, not this guard.

Run: uv run pytest ansible/tests/test_monitor_bridge_configmap_apply.py
"""

import yaml
from _helpers import REPO

ROLE = REPO / "ansible" / "roles" / "k8s" / "monitor-bridge"
TASK_NAME = "Apply the script ConfigMap"
ANNOTATION_CAP = 262144


def _apply_task(tasks):
    matches = [t for t in tasks if t.get("name") == TASK_NAME]
    assert len(matches) == 1, f"expected one {TASK_NAME!r} task, found {len(matches)}"
    return matches[0]


def _apply_cmd(task):
    module = task.get("ansible.builtin.command") or task.get("command")
    assert module, f"{TASK_NAME!r} is not an ansible.builtin.command task"
    return " ".join(module["cmd"].split())


def _runtime_module_bytes():
    files = ROLE / "files"
    return sum(
        p.stat().st_size
        for p in files.glob("*.py")
        if not p.name.startswith("test_") and p.name != "conftest.py"
    )


def test_the_configmap_is_applied_server_side():
    tasks = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text())
    cmd = _apply_cmd(_apply_task(tasks))
    assert "--server-side" in cmd, (
        f"{TASK_NAME!r} runs `{cmd}` — client-side apply re-stores the object in an "
        "annotation capped at 262144 bytes, which the bridge's modules exceed"
    )
    assert "--force-conflicts" in cmd, (
        "the data keys were owned by the client-side field manager before the switch; "
        "without --force-conflicts the first server-side apply is rejected"
    )


def test_the_reason_still_holds():
    """The modules are large enough that the client-side form would be refused."""
    # A JSON-escaped copy of the source lands in the annotation, so the raw byte count is a
    # lower bound on what client-side apply would store. Pin that it is within a factor of the
    # cap — if this ever fails, the source shrank and the comment in tasks/main.yml is stale.
    assert _runtime_module_bytes() > ANNOTATION_CAP // 2


def test_checker_rejects_a_client_side_apply():
    bad = [
        {
            "name": TASK_NAME,
            "ansible.builtin.command": {
                "cmd": "k3s kubectl apply -f /etc/rancher/k3s/monitor-bridge/configmap.yaml"
            },
        }
    ]
    assert "--server-side" not in _apply_cmd(_apply_task(bad))
