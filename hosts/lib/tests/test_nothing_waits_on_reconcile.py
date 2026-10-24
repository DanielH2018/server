"""No shipped unit waits on reconcile (H4, *The restart contract*, H4.1c).

Reconcile restarts each unit in a restart set and waits for the job to finish. A unit ordered
`After=homelab-reconcile.service` would wait for the run that is waiting for it, until a timeout
ended both. So no unit in any host package carries that ordering, and `homelab-secrets.service`
names reconcile in no ordering or requirement directive at all.

The census renders every package under `hosts/` and reads each unit and drop-in it ships. Its
named members grow with the packages: `homelab-reconcile.service` in every host package,
`homelab-secrets.service` from H4.2, and `nut-monitor`'s drop-in from H4.3c.

Run: uv run pytest hosts/lib/tests/test_nothing_waits_on_reconcile.py
"""

from pathlib import PurePosixPath

import pytest
from lib.repo_paths import REPO
from renderer.renderer_lib import host as host_mode
from renderer.renderer_lib.host_package import Package
from validate import host_packages

RECONCILE = "homelab-reconcile.service"
SECRETS = "homelab-secrets.service"
UNIT_DIR = PurePosixPath("/usr/lib/systemd/system")
NUT_DROPIN = str(UNIT_DIR / "nut-monitor.service.d" / "50-homelab.conf")
# A directive that orders or requires another unit. Reconcile may start or order itself around
# the units it restarts, but a unit that reaches back to it closes the loop.
EDGES = frozenset(
    {
        "After",
        "Before",
        "Requires",
        "Requisite",
        "Wants",
        "BindsTo",
        "PartOf",
        "Upholds",
    }
)
# What no unit may do, and what homelab-secrets may not do either.
NO_WAIT = frozenset({"After"})


def unit_of(dst: str) -> str | None:
    """The unit a shipped file defines or extends, or None when it is no unit file."""
    path = PurePosixPath(dst)
    if path.parent == UNIT_DIR:
        return path.name
    if path.parent.parent == UNIT_DIR and path.parent.name.endswith(".d"):
        return path.parent.name.removesuffix(".d")
    return None


def waits_on_reconcile(unit: str, text: str) -> list[str]:
    """Each directive of `unit`'s file that makes it wait on, or depend on, reconcile."""
    banned = EDGES if unit == SECRETS else NO_WAIT
    found = []
    for number, line in enumerate(text.splitlines(), 1):
        key, _, value = line.partition("=")
        if key.strip() in banned and RECONCILE in value.split():
            found.append(f"{unit}:{number}: {line.strip()}")
    return found


def _rendered() -> dict[str, Package]:
    """Every package under `hosts/`, by name."""
    packages = {}
    for mode, _, name in host_packages.specs(REPO):
        render = host_mode.render_host if mode == "host" else host_mode.render_package
        package = render(name)
        packages[package.name] = package
    return packages


def census(packages: dict[str, Package]) -> list[str]:
    """Every finding over the rendered `packages`, and every named member not found."""
    problems = []
    hosts = {n: p for n, p in packages.items() if p.directory.startswith("hosts/")}
    if not hosts:
        return ["no host package rendered: the census looked at nothing"]
    for name, package in sorted(hosts.items()):
        if str(UNIT_DIR / RECONCILE) not in package.files:
            problems.append(f"{name}: ships no {RECONCILE}, a named member")
    server = hosts.get("homelab-daniel-server")
    if server and NUT_DROPIN not in server.files:
        problems.append(f"homelab-daniel-server: ships no {NUT_DROPIN}, a named member")
    for name, package in sorted(packages.items()):
        for dst, (content, _) in sorted(package.files.items()):
            if unit := unit_of(dst):
                problems += [
                    f"{name}: {found}"
                    for found in waits_on_reconcile(unit, content.decode())
                ]
    return problems


def test_no_shipped_unit_waits_on_reconcile():
    assert census(_rendered()) == []


def _package(name, directory, files):
    package = Package(name=name, directory=directory)
    package.files.update({dst: (text.encode(), 0o644) for dst, text in files.items()})
    return {name: package}


def test_a_host_package_whose_unit_waits_on_reconcile_is_flagged_by_the_census():
    files = {
        f"{UNIT_DIR}/{RECONCILE}": "[Unit]\n",
        f"{UNIT_DIR}/homelab-compose.service": f"[Unit]\nAfter={RECONCILE}\n",
    }
    packages = _package("homelab-x", "hosts/x", files)
    assert census(packages) == [
        f"homelab-x: homelab-compose.service:2: After={RECONCILE}"
    ]


def test_a_host_package_without_reconcile_or_a_census_without_hosts_is_flagged():
    packages = _package("homelab-x", "hosts/x", {f"{UNIT_DIR}/a.service": "[Unit]\n"})
    assert len(census(packages)) == 1 and RECONCILE in census(packages)[0]
    shared = _package("homelab-updater", "packages/homelab-updater", {})
    assert "looked at nothing" in census(shared)[0]


def test_a_daniel_server_package_without_the_nut_monitor_drop_in_is_flagged():
    files = {f"{UNIT_DIR}/{RECONCILE}": "[Unit]\n"}
    packages = _package("homelab-daniel-server", "hosts/daniel-server", files)
    assert census(packages) == [
        f"homelab-daniel-server: ships no {NUT_DROPIN}, a named member"
    ]
    files[NUT_DROPIN] = f"[Unit]\nWants={SECRETS}\nAfter={SECRETS}\n"
    assert census(_package("homelab-daniel-server", "hosts/daniel-server", files)) == []


def test_the_census_names_daniel_pi_and_the_updater_as_members():
    assert {"homelab-daniel-pi", "homelab-updater"} <= set(_rendered())


def test_the_secrets_unit_the_component_ships_names_reconcile_nowhere():
    source = REPO / "hosts" / "components" / "secrets-unit" / "units" / SECRETS
    assert source.is_file(), "subject gone: the secrets-unit component's unit"
    assert waits_on_reconcile(SECRETS, source.read_text()) == []


def test_a_unit_ordered_after_reconcile_is_flagged():
    text = f"[Unit]\nAfter=network.target {RECONCILE}\n"
    assert waits_on_reconcile("homelab-compose.service", text) == [
        f"homelab-compose.service:2: After=network.target {RECONCILE}"
    ]


def test_a_unit_ordered_before_reconcile_or_after_another_unit_is_clean():
    text = f"[Unit]\nBefore={RECONCILE}\nAfter=network.target {SECRETS}\n"
    assert waits_on_reconcile("homelab-compose.service", text) == []


@pytest.mark.parametrize(
    "directive", ["After", "Before", "Requires", "Wants", "BindsTo", "PartOf"]
)
def test_the_secrets_unit_naming_reconcile_in_any_edge_is_flagged(directive):
    text = f"[Unit]\n{directive}={RECONCILE}\n"
    assert waits_on_reconcile(SECRETS, text) == [
        f"{SECRETS}:2: {directive}={RECONCILE}"
    ]


@pytest.mark.parametrize(
    ("dst", "unit"),
    [
        (f"{UNIT_DIR}/nut-monitor.service.d/50-homelab.conf", "nut-monitor.service"),
        (f"{UNIT_DIR}/{SECRETS}", SECRETS),
        ("/usr/lib/homelab/homelab-reconcile", None),
        (f"{UNIT_DIR}/sub/dir/x.service", None),
    ],
)
def test_a_drop_in_belongs_to_the_unit_it_extends(dst, unit):
    assert unit_of(dst) == unit
