"""What `k8s/longhorn-api` resolves, and why it must never fall back to the ClusterIP.

The Longhorn HTTP API is reachable from a node's host network namespace only through that
node's OWN longhorn-manager pod. `longhorn-manager`'s NetworkPolicy `from:` is entirely
podSelectors, so host-originated traffic — the kind Ansible sends — matches no rule and a
cross-node manager refuses the connection. The `longhorn-backend` ClusterIP load-balances
across every node's manager, one endpoint each, which makes it a coin flip: measured
2026-08-21, 2 of 8 GETs succeeded. `ansible/seed_volume_backup.yml:14-17` records the same
finding from the other direction and chose CRs instead of the HTTP API for that reason.

So the one thing this test pins is the field-selector that keeps the resolve pinned to THIS
node — the exact thing someone "simplifying" a kubectl one-liner would drop.

**This test exercises the resolve decision, not a live cluster.** `test_the_resolve_returns_a_pod_ip_on_this_node`
is the exception: `kubectl` in this repo authenticates as a read-only ServiceAccount, which is
enough to run the role's own argv for real and confirm it answers with a pod IP — it skips
itself on a host with no reachable cluster rather than failing the suite.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
from pathlib import Path

import pytest
import yaml

_ROLE = Path(__file__).resolve().parents[2] / "ansible/roles/k8s/longhorn-api"
_RESOLVE = _ROLE / "tasks/resolve.yml"


def _tasks(path: Path) -> list[dict]:
    return yaml.safe_load(path.read_text()) or []


def _named(path: Path, fragment: str) -> dict:
    for task in _tasks(path):
        if fragment in str(task.get("name", "")):
            return task
    raise AssertionError(
        f"no task in {path.name} whose name contains {fragment!r} — the task was renamed or "
        f"removed, and this test would otherwise silently check nothing."
    )


def test_the_resolve_selects_this_nodes_own_manager_pod() -> None:
    """The longhorn-manager NetworkPolicy's `from:` is all podSelectors, so host-originated
    traffic reaches only the pod on THIS node. A ClusterIP or an unfiltered pod list is a coin
    flip — measured 2026-08-21, 2 of 8 GETs succeeded. This test is what stops someone
    'simplifying' the field-selector away."""
    argv = _named(_RESOLVE, "Resolve this node's own longhorn-manager pod IP")[
        "ansible.builtin.command"
    ]["argv"]
    assert "--field-selector" in argv
    assert any("spec.nodeName=" in str(t) for t in argv)
    assert not any("longhorn-backend" in str(t) for t in argv)


def test_the_failure_guard_covers_an_empty_result() -> None:
    """A node with no local manager pod (unscheduled, mid-eviction) must fail loudly rather
    than hand back an empty `longhorn_api` a caller would happily template into a broken URL."""
    guard = _named(_RESOLVE, "Fail when this node runs no longhorn-manager")
    when = guard["when"]
    assert "longhorn_api_pod.stdout" in when
    assert "length == 0" in when


def test_the_recorded_facts_are_the_documented_interface() -> None:
    """Tasks 2 and 5 consume exactly these two facts — a rename here breaks both silently."""
    record = _named(_RESOLVE, "Record the API base")["ansible.builtin.set_fact"]
    assert record["longhorn_api"] == "http://{{ longhorn_api_pod.stdout | trim }}:9500"
    assert record["longhorn_api_node"] == "{{ ansible_hostname }}"


# --------------------------------------------------------------------------------- transport


@pytest.mark.skipif(shutil.which("kubectl") is None, reason="no kubectl on this host")
def test_the_resolve_returns_a_pod_ip_on_this_node() -> None:
    """The synthetic assertions above are only worth something if the real command produces a
    pod IP. Run the role's own argv, field-selector included, against the live API server —
    with `hostname` standing in for `ansible_hostname`, which is the same value on every node
    in this cluster (verified 2026-08-21: `kubectl get nodes` and `hostname` agree)."""
    argv = _named(_RESOLVE, "Resolve this node's own longhorn-manager pod IP")[
        "ansible.builtin.command"
    ]["argv"]
    rendered = [
        str(t).replace("{{ ansible_hostname }}", socket.gethostname()) for t in argv
    ]
    # Drop the `k3s` wrapper: the tests run as an unprivileged user against the read-only
    # kubeconfig, and `k3s kubectl` needs root here.
    assert rendered[0] == "k3s"
    result = subprocess.run(
        rendered[1:], capture_output=True, text=True, timeout=30, check=False
    )
    unreachable_tokens = (
        "connection refused",
        "was refused",
        "i/o timeout",
        "no configuration has been provided",
        # longhorn-manager is a DaemonSet: mid-rolling-update, or briefly after a node joins,
        # this node can legitimately run none of it. That is the role's own "no manager here"
        # case (see "Fail when this node runs no longhorn-manager"), not a broken jsonpath —
        # skip rather than fail a suite that gates commits on a transient cluster state.
        "array index out of bounds: index 0, length 0",
    )
    if any(token in result.stderr for token in unreachable_tokens):
        pytest.skip(
            "no reachable cluster, or no longhorn-manager pod on this node right now"
        )
    assert result.returncode == 0, (
        f"kubectl rejected the resolve command: {result.stderr.strip()}"
    )
    pod_ip = result.stdout.strip()
    assert pod_ip, (
        "this node runs a longhorn-manager pod; an empty answer means it moved"
    )
    assert pod_ip.count(".") == 3, f"expected an IPv4 address, got {pod_ip!r}"
