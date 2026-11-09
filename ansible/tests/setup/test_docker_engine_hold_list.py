"""The shared Docker engine package list.

A dist-upgrade that replaces containerd.io, docker-ce and the plugins on daniel-pi with every
container running kills autoheal's shim, makes ssh refuse connections for ~20 minutes, and
leaves docker-proxy unhealthy behind a stale socket bind-mount until a hand redeploy
recreates it (#1961). `live-restore` keeps containers up across a dockerd restart; it does
nothing for a shim binary swapped under them.

homelab-docker-engine.service installs, holds and moves the engine, and
`hosts/lib/tests/test_pi_docker_component.py` holds its pins to the shared list. No setup apply
upgrades a host's packages.

Run: uv run pytest ansible/tests/setup/test_docker_engine_hold_list.py
"""

from lib import yaml_fast
from _helpers import ALL_VARS

PACKAGES_VAR = "docker_engine_packages"
# The census must keep finding these two. containerd.io is the one whose upgrade swaps the
# shim; docker-ce is the one whose postinst restarts the daemon and the socket.
MUST_BE_HELD = frozenset({"docker-ce", "containerd.io"})


def test_the_shared_package_list_names_the_engine():
    packages = yaml_fast.safe_load(ALL_VARS.read_text()).get(PACKAGES_VAR)
    assert isinstance(packages, list), (
        f"{PACKAGES_VAR} is not a list in group_vars/all.yml"
    )
    missing = MUST_BE_HELD - set(packages)
    assert not missing, f"{PACKAGES_VAR} no longer lists {sorted(missing)}"
