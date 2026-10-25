"""Every read `k3s_gates.py` makes as `k3s-plan` has an operator-only `can-i` command.

Impersonation needs more than the read-only ServiceAccount holds, so the operator runs
`kubectl auth can-i --as=...:k3s-plan` before the Plan first selects a node (H6.3c). The
commands are derived from the script's own API path constants, so a read added to the script
without a row here fails, and `docs/k3s-upgrade.md` must print each command verbatim.

Run: uv run pytest hosts/lib/tests/test_k3s_gates_operator_reads.py
"""

import re

import pytest
from lib.repo_paths import REPO

import k3s_gates

TEMPLATE = (
    "cd /home/ubuntu/server && sudo k3s kubectl auth can-i {verb} {what} "
    "--as=system:serviceaccount:system-upgrade:k3s-plan{ns}"
)
# path constant -> (verb, resource, namespace), or None for a read only the `host` mode makes,
# as the read-only account (`homelab-readonly`), which the H6.3b verification already covers.
READS: dict[str, tuple[str, str, str] | None] = {
    "VOLUMES_PATH": ("list", "volumes.longhorn.io", ""),
    "BACKUPS_PATH": ("list", "backups.longhorn.io", ""),
    "NODES_PATH": ("list", "nodes", ""),
    "COREDNS_PATH": ("get", "deployments.apps/coredns", "kube-system"),
    "LEASE_PATH": (
        "get",
        "leases.coordination.k8s.io/kube-controller-manager",
        "kube-system",
    ),
    "PLANS_PATH": ("get", "plans.upgrade.cattle.io/k3s-server", ""),
    "JOBS_PATH": None,
    "KUSTOMIZATIONS_PATH": None,
}


def path_constants(names) -> set[str]:
    return {n for n in names if re.fullmatch(r"[A-Z_]+_PATH", n)}


def commands() -> list[str]:
    return [
        TEMPLATE.format(verb=row[0], what=row[1], ns=f" -n {row[2]}" if row[2] else "")
        for row in READS.values()
        if row
    ]


def test_every_path_constant_the_script_reads_is_classified():
    """A new `*_PATH` constant fails here until it gets a command or a host-only mark."""
    constants = path_constants(vars(k3s_gates))
    assert constants == set(READS), (
        f"unclassified: {sorted(constants - set(READS))}; "
        f"stale: {sorted(set(READS) - constants)}"
    )


def test_the_flux_source_reads_stay_host_only():
    assert set(k3s_gates._SOURCE_PATHS) == {"OCIRepository", "GitRepository"}


def test_the_check_goes_red_on_a_new_read():
    """The reject half: one more path constant leaves the classification incomplete."""
    names = [*vars(k3s_gates), "PODS_PATH"]
    assert path_constants(names) != set(READS)


def test_the_commands_the_check_must_find_are_present():
    got = commands()
    assert len(got) == 6
    assert any("list volumes.longhorn.io" in c for c in got)
    assert any(c.endswith("k3s-plan -n kube-system") for c in got)


@pytest.mark.parametrize("command", commands())
def test_the_runbook_prints_each_command(command):
    assert command in (REPO / "docs" / "k3s-upgrade.md").read_text()
