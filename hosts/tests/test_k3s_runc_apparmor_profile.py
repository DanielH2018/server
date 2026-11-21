"""Each k3s node ships the AppArmor profile that names k3s's runc.

Ubuntu 26.04 stacks every container's label with `unconfined` when the runtime that starts it
runs under the bare `unconfined` label. The stacked label fails the container profile's own
peer rules, so a signal between two processes of one container is denied: rsync never ends a
local copy, `timeout` never kills, and Traefik's and Authelia's init containers hang. Nothing
goes red on the host. The package is the only thing that puts the profile on a rebuilt node.
"""

import re
import shutil
from pathlib import Path

import pytest
from _host_packages import HOSTS, package, shipped_text
from lib import yaml_fast
from lib.proc_testing import run
from lib.repo_paths import REPO

PROFILE = "/etc/apparmor.d/homelab-k3s-runc"
K3S_COMPONENTS = {"k3s-server", "k3s-agent"}
DATA = "/var/lib/rancher/k3s/data"
# k3s unpacks its runc under the sha256 of the bundle it came from.
K3S_RUNC = f"{DATA}/{'3' * 64}/bin/runc"
NOT_K3S_RUNC = ("/usr/sbin/runc", f"{DATA}/3/extra/bin/runc", f"{DATA}/3/bin/runc-old")
ABI = Path("/etc/apparmor.d/abi/4.0")


def components(host: str) -> set[str]:
    spec = yaml_fast.safe_load((REPO / "hosts" / host / "package.yml").read_text())
    return set(spec["components"])


K3S_NODES = [host for host in HOSTS if components(host) & K3S_COMPONENTS]


@pytest.fixture(scope="module")
def profile() -> str:
    """The profile's text. The component ships one file, so one node's copy stands for both."""
    return shipped_text(K3S_NODES[0], PROFILE)


def attaches(attachment: str, path: str) -> bool:
    """Whether an AppArmor attachment of literals and `*` matches `path`. `*` stops at `/`."""
    pattern = "[^/]*".join(re.escape(part) for part in attachment.split("*"))
    return re.fullmatch(pattern, path) is not None


def test_both_cluster_nodes_are_checked():
    assert set(K3S_NODES) == {"daniel-box", "daniel-server"}


@pytest.mark.parametrize("host", HOSTS)
def test_only_a_k3s_node_ships_the_profile(host):
    assert (PROFILE in package(host).files) == (host in K3S_NODES)


@pytest.mark.parametrize("host", K3S_NODES)
def test_a_changed_profile_is_loaded_by_reconcile(host):
    assert package(host).activations[PROFILE] == "reload:apparmor.service"


def test_the_profile_attaches_to_k3s_runc_alone_and_confines_nothing(profile):
    found = re.search(r"^profile \S+ (\S+) flags=\(([^)]*)\) \{$", profile, re.M)
    assert found, "the file declares no profile with an attachment and flags"
    attachment, flags = found.groups()

    assert attaches(attachment, K3S_RUNC)
    assert not [path for path in NOT_K3S_RUNC if attaches(attachment, path)]
    assert flags.split(",") == ["unconfined"]


def test_the_profile_grants_no_user_namespace(profile):
    rules = [line.split("#")[0].strip() for line in profile.splitlines()]

    assert not [rule for rule in rules if rule.startswith("userns")]


@pytest.mark.skipif(
    not (shutil.which("apparmor_parser") and ABI.exists()),
    reason="needs apparmor_parser and the 4.0 policy ABI the profile declares",
)
def test_apparmor_parser_accepts_the_profile(profile, tmp_path):
    candidate = tmp_path / "homelab-k3s-runc"
    candidate.write_text(profile)

    result = run(
        ["apparmor_parser", "--skip-kernel-load", "--skip-cache", str(candidate)]
    )

    assert result.returncode == 0, result.stderr
