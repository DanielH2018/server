"""Each k3s node gives k3s's runc the AppArmor label `runc`.

Ubuntu 26.04 stacks every container's label with `unconfined` when the runtime that starts it
runs under the bare `unconfined` label. The stacked label fails the container profile's own
peer rules, so a signal between two processes of one container is denied: rsync never ends a
local copy, `timeout` never kills, and Traefik's and Authelia's init containers hang. A runtime
under any name but `runc` or `crun` fails the other way: the container profile refuses its
signals, so no container stops. Nothing goes red on the host in either case.
"""

import re
import shutil
from pathlib import Path

import pytest
from _host_packages import HOSTS, package
from lib import yaml_fast
from lib.proc_testing import run
from lib.repo_paths import REPO

UBUNTU_PROFILE = "/etc/apparmor.d/runc"
# The opening line of the profile the `apparmor` package ships, on 24.04 and 26.04 alike.
UBUNTU_OPENING = "profile runc /usr/{bin,sbin}/runc flags=(unconfined) {"
K3S_COMPONENTS = {"k3s-server", "k3s-agent"}
DATA = "/var/lib/rancher/k3s/data"
# k3s unpacks its runc under the sha256 of the bundle it came from.
K3S_RUNC = f"{DATA}/{'3' * 64}/bin/runc"
ATTACHED = (K3S_RUNC, "/usr/sbin/runc", "/usr/bin/runc")
NOT_ATTACHED = (f"{DATA}/3/extra/bin/runc", f"{DATA}/3/bin/runc-old", "/usr/sbin/crun")
# The labels containerd's container profile takes a signal from, besides its own daemon's.
SIGNAL_PEERS = {"unconfined", "runc", "crun"}


def components(host: str) -> set[str]:
    spec = yaml_fast.safe_load((REPO / "hosts" / host / "package.yml").read_text())
    return set(spec["components"])


K3S_NODES = [host for host in HOSTS if components(host) & K3S_COMPONENTS]


def edits(host: str) -> list[dict]:
    return [e for e in package(host).lines if e["path"] == UBUNTU_PROFILE]


def opening(host: str) -> re.Match:
    (edit,) = edits(host)
    found = re.fullmatch(r"profile (\S+) (\S+) flags=\(([^)]*)\) \{", edit["line"])
    assert found, (
        f"{host}: {edit['line']!r} opens no profile with an attachment and flags"
    )
    return found


def alternatives(pattern: str) -> list[str]:
    """`pattern` with its innermost-first `{a,b}` groups expanded."""
    group = re.search(r"\{([^{}]*)\}", pattern)
    if not group:
        return [pattern]
    return [
        expanded
        for choice in group.group(1).split(",")
        for expanded in alternatives(
            pattern[: group.start()] + choice + pattern[group.end() :]
        )
    ]


def attaches(attachment: str, path: str) -> bool:
    """Whether an AppArmor attachment of literals, `{a,b}` and `*` matches `path`.

    `*` stops at `/`, as it does in AppArmor.
    """
    return any(
        re.fullmatch("[^/]*".join(map(re.escape, each.split("*"))), path)
        for each in alternatives(attachment)
    )


def test_both_cluster_nodes_are_checked():
    assert set(K3S_NODES) == {"daniel-box", "daniel-server"}


@pytest.mark.parametrize("host", HOSTS)
def test_only_a_k3s_node_edits_the_runc_profile(host):
    assert bool(edits(host)) == (host in K3S_NODES)


@pytest.mark.parametrize("host", K3S_NODES)
def test_the_edit_replaces_the_opening_line_ubuntu_ships(host):
    (edit,) = edits(host)

    assert re.search(edit["regexp"], UBUNTU_OPENING)
    assert package(host).activations[UBUNTU_PROFILE] == "reload:apparmor.service"


@pytest.mark.parametrize("host", K3S_NODES)
def test_k3s_runc_runs_under_a_label_a_container_takes_signals_from(host):
    name, _, flags = opening(host).groups()

    assert name in SIGNAL_PEERS - {"unconfined"}
    assert flags.split(",") == ["unconfined"]


@pytest.mark.parametrize("host", K3S_NODES)
def test_the_profile_attaches_to_k3s_runc_and_keeps_the_host_runc(host):
    attachment = opening(host).group(2)

    assert [path for path in ATTACHED if attaches(attachment, path)] == list(ATTACHED)
    assert not [path for path in NOT_ATTACHED if attaches(attachment, path)]


@pytest.mark.skipif(
    not (shutil.which("apparmor_parser") and Path(UBUNTU_PROFILE).exists()),
    reason="needs apparmor_parser and the runc profile the apparmor package ships",
)
@pytest.mark.parametrize("host", K3S_NODES)
def test_apparmor_parser_accepts_the_edited_profile(host, tmp_path):
    (edit,) = edits(host)
    shipped = Path(UBUNTU_PROFILE).read_text().splitlines()
    candidate = tmp_path / "runc"
    candidate.write_text(
        "\n".join(
            edit["line"] if re.search(edit["regexp"], line) else line
            for line in shipped
        )
        + "\n"
    )

    result = run(
        ["apparmor_parser", "--skip-kernel-load", "--skip-cache", str(candidate)]
    )

    assert result.returncode == 0, result.stderr
