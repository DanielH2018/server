"""The shared Docker engine package list, and the teardown that releases its hold.

A dist-upgrade that replaces containerd.io, docker-ce and the plugins on daniel-pi with every
container running kills autoheal's shim, makes ssh refuse connections for ~20 minutes, and
leaves docker-proxy unhealthy behind a stale socket bind-mount until a hand redeploy
recreates it (#1961). `live-restore` keeps containers up across a dockerd restart; it does
nothing for a shim binary swapped under them.

homelab-docker-engine.service installs, holds and moves the engine, and
`hosts/lib/tests/test_pi_docker_component.py` holds its pins to the shared list. No setup apply
upgrades a host's packages. docker_install's teardown unholds before it purges, because apt with
-y refuses to change a held package.

Run: uv run pytest ansible/tests/setup/test_docker_engine_hold_list.py
"""

from pathlib import Path

from lib import yaml_fast
from _helpers import ALL_VARS, SETUP_ROLES

TEARDOWN = SETUP_ROLES / "docker_install" / "tasks" / "teardown.yml"

PACKAGES_VAR = "docker_engine_packages"
# The census must keep finding these two. containerd.io is the one whose upgrade swaps the
# shim; docker-ce is the one whose postinst restarts the daemon and the socket.
MUST_BE_HELD = frozenset({"docker-ce", "containerd.io"})


def load_tasks(path: Path) -> list[dict]:
    return [
        t for t in yaml_fast.safe_load(path.read_text()) or [] if isinstance(t, dict)
    ]


def index_of(tasks: list[dict], predicate) -> int | None:
    for i, task in enumerate(tasks):
        if predicate(task):
            return i
    return None


def selection_task(task: dict, selection: str) -> bool:
    sel = task.get("ansible.builtin.dpkg_selections")
    return (
        isinstance(sel, dict)
        and sel.get("selection") == selection
        and task.get("loop") == "{{ " + PACKAGES_VAR + " }}"
    )


def apt_names(task: dict) -> str | list | None:
    apt = task.get("ansible.builtin.apt")
    return apt.get("name") if isinstance(apt, dict) else None


def test_the_shared_package_list_names_the_engine():
    packages = yaml_fast.safe_load(ALL_VARS.read_text()).get(PACKAGES_VAR)
    assert isinstance(packages, list), (
        f"{PACKAGES_VAR} is not a list in group_vars/all.yml"
    )
    missing = MUST_BE_HELD - set(packages)
    assert not missing, f"{PACKAGES_VAR} no longer lists {sorted(missing)}"


def test_teardown_unholds_before_it_purges():
    tasks = load_tasks(TEARDOWN)
    unhold = index_of(tasks, lambda t: selection_task(t, "install"))
    purge = index_of(
        tasks,
        lambda t: (
            isinstance(t.get("ansible.builtin.apt"), dict)
            and t["ansible.builtin.apt"].get("state") == "absent"
            and PACKAGES_VAR in str(apt_names(t))
        ),
    )
    assert purge is not None, "teardown.yml no longer purges from the shared list"
    assert unhold is not None, (
        "teardown.yml purges held packages without releasing the hold; apt -y refuses that "
        "unless told --allow-change-held-packages"
    )
    assert unhold < purge, "the unhold runs after the purge it exists to permit"


# ── The rejecting halves ──────────────────────────────────────────────────────────────────


def test_an_unhold_of_another_list_is_detected():
    task = {
        "ansible.builtin.dpkg_selections": {"selection": "install"},
        "loop": "{{ other_packages }}",
    }
    assert not selection_task(task, "install")
