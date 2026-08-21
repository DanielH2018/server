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

**These tests exercise the resolve decision, not a live cluster.** `test_the_resolve_returns_a_pod_ip_on_this_node`
is the exception: `kubectl` in this repo authenticates as a read-only ServiceAccount, which is
enough to run the role's own argv for real and check its answer against an independently
queried ground truth. It skips only when the cluster itself is unreachable, or when the ground
truth itself shows this node running no manager pod right now — never when the command under
test merely disagrees with that ground truth, which is a real failure, not a reason to skip.
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
    # The exact templated token, not just "some nodeName clause": a hardcoded node name
    # ("spec.nodeName=daniel-server") satisfies every weaker check here and resolves the
    # WRONG node's manager — verified 2026-08-21, all four tests in this file stayed green
    # against that mutation, including the live one, which happily returned the other node's
    # pod IP. Node-locality is the entire reason this role exists, so it gets a pinned assert.
    assert "spec.nodeName={{ ansible_hostname }}" in argv
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
    assert record["longhorn_api"] == (
        "http://{{ (longhorn_api_pod.stdout | trim).split(' ')[0] }}:9500"
    )
    assert record["longhorn_api_node"] == "{{ ansible_hostname }}"


# --------------------------------------------------------------------------------- transport


_UNREACHABLE_TOKENS = (
    "connection refused",
    "was refused",
    "i/o timeout",
    "no configuration has been provided",
)


def _ground_truth_manager_ips() -> dict[str, str] | None:
    """An UNFILTERED listing of every longhorn-manager pod's node and IP, read with the
    correct, un-mutated label — independent of the role's own argv under test. `None` means
    the cluster itself is unreachable; the caller distinguishes that from "this node has none"
    using this result, not from kubectl's stderr on the (possibly broken) command under test —
    which is the discrimination the field-selector-typo mutation above exposed as missing:
    with `[*]` in place, a broken label and a genuinely absent node-local pod both produce rc=0
    and empty stdout on the command under test, so they cannot be told apart from that alone.
    """
    result = subprocess.run(
        [
            "kubectl",
            "-n",
            "longhorn-system",
            "get",
            "pod",
            "-l",
            "app=longhorn-manager",
            "-o",
            'jsonpath={range .items[*]}{.spec.nodeName}{"="}{.status.podIP}{"\\n"}{end}',
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0 or any(t in result.stderr for t in _UNREACHABLE_TOKENS):
        return None
    ips: dict[str, str] = {}
    for line in result.stdout.strip().splitlines():
        node, _, ip = line.partition("=")
        if node and ip:
            ips[node] = ip
    return ips


@pytest.mark.skipif(shutil.which("kubectl") is None, reason="no kubectl on this host")
def test_the_resolve_returns_a_pod_ip_on_this_node() -> None:
    """The synthetic assertions above are only worth something if the real command produces a
    pod IP. Run the role's own argv, field-selector included, against the live API server —
    with `hostname` standing in for `ansible_hostname`, which is the same value on every node
    in this cluster (verified 2026-08-21: `kubectl get nodes` and `hostname` agree).

    A ground-truth listing (an independent, correctly-labelled query) decides what counts as
    "reachable but this node has none" versus a real failure of the command under test — a
    mutated label or a hardcoded wrong node name both produce a result the ground truth
    disagrees with, and that disagreement is what fails this test rather than skipping it.
    """
    ground_truth = _ground_truth_manager_ips()
    if ground_truth is None:
        pytest.skip("no reachable cluster")
    this_node = socket.gethostname()
    if this_node not in ground_truth:
        pytest.skip(f"no longhorn-manager pod on {this_node} right now")
    expected_ip = ground_truth[this_node]

    argv = _named(_RESOLVE, "Resolve this node's own longhorn-manager pod IP")[
        "ansible.builtin.command"
    ]["argv"]
    rendered = [str(t).replace("{{ ansible_hostname }}", this_node) for t in argv]
    # Drop the `k3s` wrapper: the tests run as an unprivileged user against the read-only
    # kubeconfig, and `k3s kubectl` needs root here.
    assert rendered[0] == "k3s"
    result = subprocess.run(
        rendered[1:], capture_output=True, text=True, timeout=30, check=False
    )
    assert result.returncode == 0, (
        f"ground truth found a longhorn-manager pod on {this_node}, but the role's own "
        f"command failed: {result.stderr.strip()}"
    )
    pod_ip = result.stdout.strip().split(" ")[0]
    assert pod_ip == expected_ip, (
        f"the role's command returned {pod_ip!r}, but the independent listing says "
        f"{this_node}'s manager pod is at {expected_ip!r} — the field selector or label "
        f"resolved the wrong pod, or none"
    )
