"""What `k8s/longhorn-api` resolves, and why it must never fall back to the ClusterIP.

The Longhorn HTTP API is reachable from a node's host network namespace only through that
node's OWN longhorn-manager pod. `longhorn-manager`'s NetworkPolicy `from:` is entirely
podSelectors, so host-originated traffic — the kind Ansible sends — matches no rule and a
cross-node manager refuses the connection. The `longhorn-backend` ClusterIP load-balances
across every node's manager, one endpoint each, which makes it a coin flip: 2 of 8
GETs succeeded. `ansible/seed_volume_backup.yml:14-17` records the same
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

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from lib.proc_testing import fake_bin, path_with
from _helpers import K8S_ROLES
from _helpers import REPO as _REPO_ROOT
from _helpers import load_tasks as _tasks
from _helpers import task_named
from longhorn_manager import ready_manager_ip

_ROLE = K8S_ROLES / "longhorn-api"
_RESOLVE = _ROLE / "tasks/resolve.yml"


def _named(path: Path, fragment: str) -> dict:
    return task_named(_tasks(path), fragment)


def test_the_resolve_selects_this_nodes_own_manager_pod() -> None:
    """The longhorn-manager NetworkPolicy's `from:` is all podSelectors, so host-originated
    traffic reaches only the pod on THIS node. A ClusterIP or an unfiltered pod list is a coin
    flip — 2 of 8 GETs succeeded. This test is what stops someone
    'simplifying' the field-selector away."""
    argv = _named(_RESOLVE, "Resolve this node's own longhorn-manager pod IP")[
        "ansible.builtin.command"
    ]["argv"]
    assert "--field-selector" in argv
    # The exact templated token, not just "some nodeName clause": a hardcoded node name
    # ("spec.nodeName=daniel-server") satisfies every weaker check here and resolves the
    # WRONG node's manager — every other test in this file stays green against that mutation,
    # including the live one, which happily returns the other node's pod IP. Node-locality is the entire reason this role exists, so it gets a pinned assert.
    assert "spec.nodeName={{ ansible_hostname }}" in argv
    assert not any("longhorn-backend" in str(t) for t in argv)
    # The whole pod list as JSON, so the readiness rule can read containerStatuses (#3736).
    assert argv[-2:] == ["-o", "json"]


def test_the_resolve_picks_a_ready_pod_through_the_shared_rule() -> None:
    """The jsonpath this replaced returned every pod on the node, ready or not (#3736)."""
    pick = _named(_RESOLVE, "Pick this node's ready longhorn-manager pod")
    assert pick["ansible.builtin.set_fact"]["longhorn_api_ip"] == (
        "{{ (longhorn_api_pod.stdout | from_json)['items'] | "
        "ready_manager_ip(ansible_hostname) }}"
    )


def test_the_failure_guard_covers_an_empty_result() -> None:
    """A node with no local manager pod (unscheduled, mid-eviction) must fail loudly rather
    than hand back an empty `longhorn_api` a caller would happily template into a broken URL —
    unless the caller opted into soft mode, which the next test covers."""
    guard = _named(_RESOLVE, "Fail when this node runs no ready longhorn-manager")
    when = guard["when"]
    assert isinstance(when, list)
    assert "longhorn_api_ip | length == 0" in when
    assert "longhorn_api_required | bool" in when


def test_soft_mode_records_the_miss_instead_of_failing() -> None:
    """`longhorn_api_required: false` is the ONLY supported way to make an absent manager non-fatal.

    `ignore_errors` on the include does not work — see
    `test_longhorn_api_soft_mode_survives_no_manager` below, which proves the mechanism rather than
    the YAML shape.
    """
    task = _named(
        _RESOLVE, "Record that no ready longhorn-manager pod exists on this node"
    )
    when = task["when"]
    assert "not (longhorn_api_required | bool)" in when
    assert any("length == 0" in str(c) for c in when)
    assert task["ansible.builtin.set_fact"]["longhorn_api_resolved"] is False


def test_the_success_path_also_records_resolved_true() -> None:
    """A caller in soft mode needs one fact to branch on regardless of outcome — a success that
    only sets `longhorn_api`/`longhorn_api_node` would leave `longhorn_api_resolved` undefined
    on the path that actually worked."""
    task = _named(_RESOLVE, "Record the API base")
    assert task["when"] == "longhorn_api_ip | length > 0"
    assert task["ansible.builtin.set_fact"]["longhorn_api_resolved"] is True


def test_the_recorded_facts_are_the_documented_interface() -> None:
    """Tasks 2 and 5 consume exactly these two facts — a rename here breaks both silently."""
    record = _named(_RESOLVE, "Record the API base")["ansible.builtin.set_fact"]
    assert record["longhorn_api"] == "http://{{ longhorn_api_ip }}:9500"
    assert record["longhorn_api_node"] == "{{ ansible_hostname }}"


_UNREACHABLE_TOKENS = (
    "connection refused",
    "was refused",
    "i/o timeout",
    "no configuration has been provided",
)


def _ground_truth_manager_ips() -> dict[str, str] | None:
    """Every READY longhorn-manager pod's node and IP, read with the correct, un-mutated
    label — independent of the role's own argv under test and of the filter it calls. Ready
    is decided here from a jsonpath read of the phase and the container readiness flags,
    not by `ready_manager_ip`, so a bug in that function cannot agree with itself. `None` means
    the cluster itself is unreachable; the caller distinguishes that from "this node has none"
    using this result, not from kubectl's stderr on the (possibly broken) command under test —
    which is the discrimination the field-selector-typo mutation above exposed as missing:
    a broken label and a genuinely absent node-local pod both produce rc=0 and an empty item
    list on the command under test, so they cannot be told apart from that alone.
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
            'jsonpath={range .items[*]}{.spec.nodeName}{"="}{.status.podIP}{"="}'
            '{.status.phase}{"="}{.status.containerStatuses[*].ready}{"\\n"}{end}',
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0 or any(t in result.stderr for t in _UNREACHABLE_TOKENS):
        return None
    # First ready match wins on a duplicate node, as `ready_manager_ip` takes the first — if a
    # node ever runs two ready manager pods, the role and this ground truth must agree on which
    # one is "the" answer, or a real multi-pod state fails this test on a disagreement that is
    # not a bug.
    ips: dict[str, str] = {}
    for line in result.stdout.strip().splitlines():
        node, ip, phase, readies = (line.split("=", 3) + ["", "", ""])[:4]
        ready = phase == "Running" and readies and set(readies.split()) == {"true"}
        if node and ip and ready and node not in ips:
            ips[node] = ip
    return ips


@pytest.mark.skipif(shutil.which("kubectl") is None, reason="no kubectl on this host")
def test_the_resolve_returns_a_pod_ip_on_this_node() -> None:
    """The synthetic assertions above are only worth something if the real command produces a
    pod IP. Run the role's own argv, field-selector included, against the live API server —
    with `hostname` standing in for `ansible_hostname`, which is the same value on every node
    in this cluster (`kubectl get nodes` and `hostname` agree).

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
        pytest.skip(f"no ready longhorn-manager pod on {this_node} right now")
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
    pod_ip = ready_manager_ip(json.loads(result.stdout)["items"], this_node)
    assert pod_ip == expected_ip, (
        f"the role's command returned {pod_ip!r}, but the independent listing says "
        f"{this_node}'s manager pod is at {expected_ip!r} — the field selector or label "
        f"resolved the wrong pod, or none"
    )


#
# `ignore_errors: true` on a dynamic `include_role` does not catch a failure of a task the
# include pulls in — only a failure of the include statement itself. That is documented Ansible
# behaviour, and k8s/volume-snapshot's first cut of the detached-volume attach shipped exactly
# that mistake: `ignore_errors` on the `include_role: {name: k8s/longhorn-api, ...}` task, which
# a reviewer proved does nothing by running the REAL, unmodified role through a scratch play
# with `k3s` stubbed to report no manager pod — the play still aborted at "Fail when this node
# runs no longhorn-manager", the include's `ignore_errors` notwithstanding.
#
# These two tests run that same proof against the actual fix: `longhorn_api_required: false`,
# read INSIDE resolve.yml, so the role itself chooses not to raise rather than asking a caller's
# `ignore_errors` to catch something it structurally cannot. `become: true` on the pod-IP read
# is satisfied by a passthrough `sudo` replacement rather than real privilege escalation — there
# is nothing here that needs root, and the test sandbox has no passwordless sudo to use.


def _manager_pod(*, ready: bool) -> dict:
    return {
        "spec": {"nodeName": "testnode"},
        "status": {
            "phase": "Running",
            "podIP": "10.42.0.7",
            "containerStatuses": [{"ready": ready}],
        },
    }


def _run_longhorn_api_scratch_play(
    *, required: bool, pods: tuple[dict, ...] = ()
) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        # `fake_become` is a `sudo`/`become_exe` passthrough: it finds the trailing
        # `-c '<command>'` the sudo become plugin always builds and execs it directly, as the
        # current (unprivileged) user. Nothing the resolved task runs needs real root — it only
        # needs `become: true` to not block on a password prompt this sandbox cannot answer.
        bin_dir = fake_bin(
            tmp_path / "bin",
            k3s="#!/bin/sh\ncat <<'EOF'\n%s\nEOF\n" % json.dumps({"items": list(pods)}),
            fake_become=(
                "#!/bin/sh\n"
                'last=""\n'
                'prev=""\n'
                'for a in "$@"; do prev="$last"; last="$a"; done\n'
                'if [ "$prev" = "-c" ]; then exec /bin/sh -c "$last"; fi\n'
                'exec "$@"\n'
            ),
        )
        fake_become = bin_dir / "fake_become"

        required_var = "" if required else "\n          longhorn_api_required: false"
        playbook = tmp_path / "play.yml"
        playbook.write_text(
            "- hosts: localhost\n"
            "  connection: local\n"
            "  gather_facts: false\n"
            "  vars:\n"
            "    ansible_hostname: testnode\n"
            f'    ansible_become_exe: "{fake_become}"\n'
            "  tasks:\n"
            "    - name: Resolve longhorn API\n"
            "      ansible.builtin.include_role:\n"
            "        name: k8s/longhorn-api\n"
            "        tasks_from: resolve.yml\n"
            f"      vars:{required_var}\n"
            "    - name: Prove we are still alive\n"
            "      ansible.builtin.debug:\n"
            "        msg: \"SURVIVED longhorn_api_resolved={{ longhorn_api_resolved | default('undef') }}"
            " api={{ longhorn_api | default('undef') }}\"\n"
        )

        env = dict(os.environ)
        env["PATH"] = path_with(bin_dir, env=env)
        env["ANSIBLE_LOG_PATH"] = str(tmp_path / "ansible.log")
        env["ANSIBLE_NOCOLOR"] = "1"
        # The real playbooks find `filter_plugins/` beside them in `ansible/`; this one is in a
        # temp dir, and resolve.yml calls `ready_manager_ip`.
        env["ANSIBLE_FILTER_PLUGINS"] = str(_REPO_ROOT / "ansible" / "filter_plugins")
        # Pin the interpreter to the suite's own, so the play's modules import from the venv
        # the test runs in rather than whatever discovery finds first on PATH.
        env["ANSIBLE_PYTHON_INTERPRETER"] = sys.executable

        return subprocess.run(
            ["ansible-playbook", str(playbook), "-i", "localhost,"],
            cwd=_REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )


@pytest.mark.skipif(
    shutil.which("ansible-playbook") is None, reason="ansible-playbook not on PATH"
)
def test_longhorn_api_soft_mode_survives_no_manager() -> None:
    """The fix: soft mode works because resolve.yml skips its own `fail()`.

    `longhorn_api_required: false` makes an absent manager pod non-fatal because resolve.yml itself
    skips its own `fail()`, not because a caller's `ignore_errors` catches it. If this regresses
    back to relying on `ignore_errors` at the call site, this test goes red — it runs the real role,
    not a rendered expression.
    """
    result = _run_longhorn_api_scratch_play(required=False)
    assert result.returncode == 0, (
        f"soft mode must not abort the play when no longhorn-manager pod exists.\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "SURVIVED longhorn_api_resolved=False" in result.stdout


@pytest.mark.skipif(
    shutil.which("ansible-playbook") is None, reason="ansible-playbook not on PATH"
)
def test_longhorn_api_hard_mode_still_fails_by_default() -> None:
    """The control for the test above: hard mode still fails by default.

    k8s/volume-revert never sets `longhorn_api_required`, so it must keep getting today's hard
    failure. Without this, a bug that made soft mode the DEFAULT would pass the test above and
    silently defang volume-revert's fail-fast guarantee.
    """
    result = _run_longhorn_api_scratch_play(required=True)
    assert result.returncode != 0
    assert "No ready longhorn-manager pod" in result.stdout
    assert "SURVIVED" not in result.stdout


@pytest.mark.skipif(
    shutil.which("ansible-playbook") is None, reason="ansible-playbook not on PATH"
)
def test_longhorn_api_refuses_a_manager_that_is_not_ready() -> None:
    """A manager whose container is not ready is no API to send a revert to (#3736)."""
    result = _run_longhorn_api_scratch_play(
        required=True, pods=(_manager_pod(ready=False),)
    )
    assert result.returncode != 0
    assert "No ready longhorn-manager pod" in result.stdout


@pytest.mark.skipif(
    shutil.which("ansible-playbook") is None, reason="ansible-playbook not on PATH"
)
def test_longhorn_api_resolves_a_ready_manager() -> None:
    """The accepting half: the same pod, ready, becomes the API base."""
    result = _run_longhorn_api_scratch_play(
        required=True, pods=(_manager_pod(ready=True),)
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (
        "SURVIVED longhorn_api_resolved=True api=http://10.42.0.7:9500" in result.stdout
    )
