"""The Docker engine packages are held, and the hold precedes the dist-upgrade.

On 2026-09-18 initial_setup's `Update and upgrade system packages` replaced containerd.io,
docker-ce and the plugins on daniel-pi with every container running: autoheal's shim died,
ssh refused connections for ~20 minutes, and docker-proxy sat unhealthy behind a stale socket
bind-mount until a hand redeploy recreated it (#1961). `live-restore` keeps containers up
across a dockerd restart; it does nothing for a shim binary swapped under them.

Three things must stay true for the hold to do its job, and every failure is silent -- an
unheld package simply upgrades on the next run:

1. the hold set is the install set (a package added to one and not the other floats);
2. the hold task runs BEFORE the dist-upgrade in host-basics.yml, because docker_install
   runs after initial_setup in the play, so a hold set only there lands one upgrade late;
3. teardown unholds before it purges, because apt with -y refuses to change a held package.

A fourth, since #2153 pinned the versions: the install reads `docker_install_package_specs`,
whose keys (`docker_install_package_versions` in the role defaults) must be exactly the hold
list -- a package pinned and installed under a name the hold never sees floats the same way.
And no apt task under docker_install says `state: latest`, because that is the calendar
deciding the engine version; the pin decides now.

Run: uv run pytest ansible/tests/setup/test_docker_engine_is_held_before_apt_upgrade.py
"""

from pathlib import Path

from lib import yaml_fast
from _helpers import ALL_VARS, SETUP_ROLES

HOST_BASICS = SETUP_ROLES / "initial_setup" / "tasks" / "host-basics.yml"
INSTALL = SETUP_ROLES / "docker_install" / "tasks" / "install.yml"
TEARDOWN = SETUP_ROLES / "docker_install" / "tasks" / "teardown.yml"
ENGINE_UPGRADE = SETUP_ROLES / "docker_install" / "tasks" / "engine-upgrade.yml"
DEFAULTS = SETUP_ROLES / "docker_install" / "defaults" / "main.yml"

PACKAGES_VAR = "docker_engine_packages"
# The `name=version` list the install and the deliberate upgrade hand to apt, and the dict
# it is rendered from -- both in the role defaults.
SPECS_VAR = "docker_install_package_specs"
VERSIONS_VAR = "docker_install_package_versions"
# The census must keep finding these two. containerd.io is the one whose upgrade swaps the
# shim; docker-ce is the one whose postinst restarts the daemon and the socket.
MUST_BE_HELD = frozenset({"docker-ce", "containerd.io"})


def load_tasks(path: Path) -> list[dict]:
    return [
        t for t in yaml_fast.safe_load(path.read_text()) or [] if isinstance(t, dict)
    ]


def iter_tasks(tasks):
    """Every task, descending into block/rescue/always."""
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        yield task
        for key in ("block", "rescue", "always"):
            yield from iter_tasks(task.get(key))


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


def pinned_names(versions: dict) -> set[str]:
    """The package names the pin dict covers -- what the install and the upgrade hand to apt."""
    return set(versions)


def test_install_and_hold_read_the_same_list():
    """A package installed from a literal list is a package the hold never sees.

    The install reads the pinned specs; the specs are rendered from the versions dict; the
    dict's keys are the hold list. Any link missing and a package floats or is never held.
    """
    tasks = load_tasks(INSTALL)
    installs = [t for t in tasks if apt_names(t) == "{{ " + SPECS_VAR + " }}"]
    assert installs, f"install.yml does not install from {SPECS_VAR}"
    assert any(selection_task(t, "hold") for t in tasks), (
        "install.yml installs the engine and never holds it -- a fresh host floats until "
        "the next full run reaches host-basics"
    )
    defaults = yaml_fast.safe_load(DEFAULTS.read_text())
    assert SPECS_VAR in defaults and VERSIONS_VAR in defaults[SPECS_VAR], (
        f"{SPECS_VAR} is not rendered from {VERSIONS_VAR} in defaults/main.yml"
    )
    held = set(yaml_fast.safe_load(ALL_VARS.read_text())[PACKAGES_VAR])
    pinned = pinned_names(defaults[VERSIONS_VAR])
    assert pinned == held, (
        f"{VERSIONS_VAR} pins {sorted(pinned - held)} the hold never sees and omits "
        f"{sorted(held - pinned)} the hold covers"
    )


def test_no_apt_task_under_docker_install_says_latest():
    """`state: latest` is the calendar choosing the engine version; the pin chooses now."""
    latest = [
        t.get("name")
        for path in (INSTALL, ENGINE_UPGRADE, TEARDOWN)
        for t in iter_tasks(load_tasks(path))
        if isinstance(t.get("ansible.builtin.apt"), dict)
        and t["ansible.builtin.apt"].get("state") == "latest"
    ]
    assert not latest, f"apt tasks with state: latest under docker_install: {latest}"


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


def test_docker_install_no_longer_runs_a_second_host_upgrade():
    """`upgrade: true` on the cache refresh was the second path by which docker-ce moved."""
    upgrading = [
        t["name"]
        for t in load_tasks(INSTALL)
        if isinstance(t.get("ansible.builtin.apt"), dict)
        and t["ansible.builtin.apt"].get("upgrade") not in (None, "no", False)
    ]
    assert not upgrading, f"install.yml runs a host upgrade in {upgrading}"


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


def test_the_deliberate_upgrade_reholds_in_always():
    """A failed apt run must not leave the engine exposed to the next dist-upgrade."""
    tasks = load_tasks(ENGINE_UPGRADE)
    blocks = [
        t
        for t in tasks
        if any(selection_task(b, "install") for b in t.get("block", []))
    ]
    assert blocks, (
        "engine-upgrade.yml never releases the hold, so its apt task changes nothing"
    )
    assert any(selection_task(b, "hold") for b in blocks[0].get("always", [])), (
        "the unhold block has no always: re-hold, so an apt failure leaves the engine unheld"
    )
    assert any(
        t.get("community.docker.docker_compose_v2", {}).get("recreate") == "always"
        for t in iter_tasks(tasks)
    ), (
        "nothing recreates the Compose projects, so docker-proxy keeps the stale socket inode"
    )


def test_the_deliberate_upgrade_is_never_tagged():
    """Reached by name only; a bare initial_setup run must not stop the Pi's containers."""
    includes = [
        t
        for t in load_tasks(INSTALL)
        if isinstance(t.get("ansible.builtin.include_tasks"), dict)
        and t["ansible.builtin.include_tasks"].get("file") == ENGINE_UPGRADE.name
    ]
    assert includes, "install.yml does not include engine-upgrade.yml"
    assert "never" in includes[0].get("tags", []), (
        "engine-upgrade.yml is included without the never tag, so every full run stops the "
        "Pi's containers and upgrades the engine"
    )


UPGRADE_GATE = "docker_install_engine_upgrade"
MAIN = SETUP_ROLES / "docker_install" / "tasks" / "main.yml"
DEFAULTS = SETUP_ROLES / "docker_install" / "defaults" / "main.yml"


def test_the_deliberate_upgrade_is_gated_by_a_variable_no_tag_reaches():
    """`never` loses to the inherited role tag (#1998); only a variable gate survives it.

    initial_setup.yml tags the whole role `docker_install`, every task in it inherits that
    tag, and an explicitly requested tag overrides `never` -- so `--tags docker_install`
    ran the engine upgrade. The include must carry a `when:` on a variable that defaults
    to false.
    """
    includes = [
        t
        for t in load_tasks(INSTALL)
        if isinstance(t.get("ansible.builtin.include_tasks"), dict)
        and t["ansible.builtin.include_tasks"].get("file") == ENGINE_UPGRADE.name
    ]
    assert includes, "install.yml does not include engine-upgrade.yml"
    assert UPGRADE_GATE in str(includes[0].get("when", "")), (
        f"the engine-upgrade include has no `when: {UPGRADE_GATE}` gate; the role tag "
        "inherits onto it and `--tags docker_install` runs the upgrade"
    )
    defaults = yaml_fast.safe_load(DEFAULTS.read_text())
    assert defaults.get(UPGRADE_GATE) is False, (
        f"defaults/main.yml must set {UPGRADE_GATE}: false so the gate is closed unless "
        "the operator opens it with -e"
    )


def test_the_dispatcher_imports_statically_so_granular_tags_reach_their_tasks():
    """A dynamic include is a task `--tags` judges first; an untagged one is skipped whole.

    `--tags docker-daemon` reached nothing inside install.yml on 2026-09-18 (#1998).
    Tagging the include would select every task inside it instead. import_tasks inlines
    them, and each keeps its own tags.
    """
    for task in load_tasks(MAIN):
        assert "ansible.builtin.include_tasks" not in task, (
            f"main.yml task {task.get('name')!r} uses include_tasks; the granular tags "
            "inside it are unreachable -- use import_tasks"
        )
    imported = {
        t["ansible.builtin.import_tasks"]
        for t in load_tasks(MAIN)
        if "ansible.builtin.import_tasks" in t
    }
    assert imported == {INSTALL.name, TEARDOWN.name}, imported


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


def test_a_literal_package_list_is_detected():
    task = {"ansible.builtin.apt": {"name": ["docker-ce"], "state": "present"}}
    assert apt_names(task) != "{{ " + SPECS_VAR + " }}"


def test_a_pin_outside_the_hold_list_is_detected():
    """docker-ce-rootless-extras pinned and installed, but never held: it floats."""
    held = {"docker-ce", "containerd.io"}
    assert pinned_names({"docker-ce": "5:1", "containerd.io": "1"}) == held
    assert (
        pinned_names({"docker-ce": "5:1", "docker-ce-rootless-extras": "5:1"}) != held
    )


def test_a_hold_on_another_list_is_detected():
    task = {
        "ansible.builtin.dpkg_selections": {"selection": "hold"},
        "loop": "{{ other_packages }}",
    }
    assert not selection_task(task, "hold")
