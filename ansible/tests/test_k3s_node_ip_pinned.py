#!/usr/bin/env python3
"""Guards that k3s never autodetects its own node IP again.

The failure this encodes actually happened (daniel-box, 2026-08-01). The host was
multi-homed at install time — a USB ethernet adapter on 10.0.0.153 alongside eno1
on 10.0.0.215, both carrying equal-metric default routes — and k3s autodetected
the USB one, registering the node's InternalIP as 10.0.0.153.

When that adapter later disappeared, the node's address stopped existing:

    failed to validate nodeIP: node IP: "10.0.0.153" not found in the host's
    network interfaces
    dial tcp 10.0.0.153:6443: connect: no route to host

The kubernetes Service still resolved (kube-proxy's DNAT was intact) but pointed at
an endpoint on a dead address, so every pod failed to reach 10.43.0.1. Longhorn's
csi-provisioner could not create a single volume and every PVC sat Pending. It stayed
invisible for hours because `k3s kubectl` from the host talks to 127.0.0.1:6443 and
went on working the whole time.

Run: uv run pytest ansible/tests/test_k3s_node_ip_pinned.py
"""

import re
from pathlib import Path

import yaml

ANSIBLE = Path(__file__).resolve().parents[1]
K3S = ANSIBLE / "roles" / "setup" / "k3s"


def _defaults() -> dict:
    return yaml.safe_load((K3S / "defaults" / "main.yml").read_text())


# main.yml became a list of import_tasks in the 2026-08-15 split, so expand the imports —
# otherwise these assertions pass vacuously against a file holding nothing but imports.
# Both helpers below expand the SAME set, in import order: globbing tasks/*.yml instead
# would also pull in agent.yml/agent_verify.yml, which main.yml does not import and which
# the server-side assertions here are not about.
def _imported_files() -> list[Path]:
    tasks_dir = K3S / "tasks"
    return [
        tasks_dir / entry["ansible.builtin.import_tasks"]
        for entry in yaml.safe_load((tasks_dir / "main.yml").read_text()) or []
        if entry.get("ansible.builtin.import_tasks")
    ]


def _task_text() -> str:
    return "\n".join(p.read_text() for p in _imported_files())


def _tasks() -> list[dict]:
    tasks: list[dict] = []
    for path in _imported_files():
        loaded = yaml.safe_load(path.read_text()) or []
        tasks += [t for t in loaded if isinstance(t, dict)]
    return tasks


def _install_task() -> dict:
    for task in _tasks():
        cmd = task.get("ansible.builtin.command", {})
        if isinstance(cmd, dict) and "k3s-install.sh" in cmd.get("cmd", ""):
            return task
    raise AssertionError("No task runs k3s-install.sh — the role was restructured.")


def test_node_ip_is_pinned_to_the_hosts_canonical_address():
    """Autodetection is what picked the removable NIC."""
    args = _defaults()["k3s_server_args"]
    for flag in ("--node-ip", "--advertise-address"):
        assert f"{flag} {{{{ server_ip }}}}" in args, (
            f"k3s_server_args must pass `{flag} {{{{ server_ip }}}}`. Without it k3s "
            "autodetects an address, and on a multi-homed host it can pick a NIC that "
            "later goes away, taking pod-to-apiserver traffic with it."
        )


def test_k3s_version_is_pinned():
    """A reconfigure re-runs the installer; unpinned, that upgrades the control plane."""
    version = _defaults().get("k3s_version", "")
    assert re.fullmatch(r"v\d+\.\d+\.\d+\+k3s\d+", str(version)), (
        f"k3s_version must be an explicit version, got {version!r}. The install task "
        "re-runs get.k3s.io whenever k3s_server_args changes, and get.k3s.io installs "
        "latest stable unless INSTALL_K3S_VERSION is set — so an unpinned value turns "
        "editing a flag into a silent control-plane upgrade."
    )


def test_installer_is_not_guarded_on_the_binary_existing():
    """`creates:` on the binary is why the node-IP fix could not have applied."""
    task = _install_task()
    creates = task.get("args", {}).get("creates")
    assert creates is None, (
        f"The k3s install task must not carry `creates: {creates}`. Guarding on the "
        "binary means a change to k3s_server_args never reaches an installed host — the "
        "play reports ok while the systemd unit keeps its original ExecStart."
    )
    assert "when" in task, (
        "The install task needs a `when:` comparing the desired arguments against the "
        "installed unit, or it reinstalls k3s on every run."
    )


def test_role_asserts_the_registered_node_ip():
    """The runtime half: catch a wrong address on the next play, not hours later."""
    assert "k3s_node_ip.stdout == server_ip" in _task_text(), (
        "The role must assert the node's registered InternalIP equals server_ip. "
        "`kubectl` from the host keeps working over 127.0.0.1 when this is wrong, so "
        "nothing else surfaces it."
    )
