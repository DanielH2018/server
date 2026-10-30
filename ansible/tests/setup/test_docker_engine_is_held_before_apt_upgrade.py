"""The Docker engine packages are held, and the hold precedes the dist-upgrade.

A dist-upgrade that replaces containerd.io, docker-ce and the plugins on daniel-pi with every
container running kills autoheal's shim, makes ssh refuse connections for ~20 minutes, and
leaves docker-proxy unhealthy behind a stale socket bind-mount until a hand redeploy
recreates it (#1961). `live-restore` keeps containers up across a dockerd restart; it does
nothing for a shim binary swapped under them.

Two Ansible tasks keep the hold, and every failure is silent -- an unheld package simply
upgrades on the next run:

1. initial_setup's host-basics.yml holds the engine BEFORE its dist-upgrade;
2. docker_install's teardown unholds before it purges, because apt with -y refuses to change a
   held package.

Since H7.26 homelab-docker-engine.service installs and moves the engine, and
`hosts/lib/tests/test_pi_docker_component.py` holds its pins to the same hold list.

Run: uv run pytest ansible/tests/setup/test_docker_engine_is_held_before_apt_upgrade.py
"""

from pathlib import Path

from lib import yaml_fast
from _helpers import ALL_VARS, SETUP_ROLES

HOST_BASICS = SETUP_ROLES / "initial_setup" / "tasks" / "host-basics.yml"
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


def is_dist_upgrade(task: dict) -> bool:
    apt = task.get("ansible.builtin.apt")
    return isinstance(apt, dict) and apt.get("upgrade") == "dist"


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


def test_the_hold_precedes_the_dist_upgrade():
    tasks = load_tasks(HOST_BASICS)
    hold = index_of(tasks, lambda t: selection_task(t, "hold"))
    upgrade = index_of(tasks, is_dist_upgrade)
    assert upgrade is not None, (
        "host-basics.yml no longer dist-upgrades; move this guard"
    )
    assert hold is not None, (
        "host-basics.yml holds nothing before its dist-upgrade, so the next run moves "
        "containerd.io under every running container on the Pi"
    )
    assert hold < upgrade, (
        "the hold is defined after the dist-upgrade it is meant to precede"
    )
    when = tasks[hold].get("when")
    assert "has_docker" in (when if isinstance(when, list) else [when]), (
        "the hold is not gated on has_docker, so it fails on every host with no Docker "
        "(dpkg_selections errors on a name dpkg has never seen)"
    )


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


def test_a_hold_after_the_upgrade_is_detected():
    tasks = [
        {"name": "up", "ansible.builtin.apt": {"upgrade": "dist"}},
        {
            "name": "hold",
            "ansible.builtin.dpkg_selections": {"selection": "hold"},
            "loop": "{{ " + PACKAGES_VAR + " }}",
        },
    ]
    hold = index_of(tasks, lambda t: selection_task(t, "hold"))
    upgrade = index_of(tasks, is_dist_upgrade)
    assert hold is not None and upgrade is not None and hold > upgrade


def test_a_hold_on_another_list_is_detected():
    task = {
        "ansible.builtin.dpkg_selections": {"selection": "hold"},
        "loop": "{{ other_packages }}",
    }
    assert not selection_task(task, "hold")
