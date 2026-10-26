"""The persisted-output census: every output a package persists is on one named list (H4.3a).

C5 admits a persisted output only for a consumer that must start before the decrypt can run
(`docs/target-architecture/host/h4-secrets-unit.md`, *Persisted outputs*). The list has one
member, daniel-server's `upsmon.conf`. A package that persists anything else fails here and
names the output. A listed member that no package renders fails too, so a renamed output
cannot shrink the census to nothing.

Run: uv run pytest hosts/lib/tests/test_persisted_outputs_census.py
"""

from lib.repo_paths import REPO
from renderer.renderer_lib import host as host_mode
from renderer.renderer_lib.host_outputs import OUTPUT_ROOTS

HOSTS = REPO / "hosts"
PERSISTED = {
    "homelab-daniel-server:/var/lib/homelab-secrets/etc/nut/upsmon.conf",
    "homelab-daniel-box:/var/lib/homelab-secrets/etc/nut/upsmon.conf",
}
# Packages the census must find, so a glob that stops matching cannot empty it.
MEMBERS = {
    "homelab-daniel-server",
    "homelab-daniel-box",
    "homelab-daniel-pi",
    "homelab-updater",
}


def rendered_packages() -> list[host_mode.Package]:
    """Every host package and every shared package under `hosts/`."""
    hosts = sorted(p.parent.name for p in HOSTS.glob("*/package.yml"))
    shared = sorted(p.parent.name for p in (HOSTS / "packages").glob("*/package.yml"))
    return [host_mode.render_host(h) for h in hosts] + [
        host_mode.render_package(p) for p in shared
    ]


def persisted(packages: list[host_mode.Package]) -> set[str]:
    """`<package>:<installed path>` for each output a package persists."""
    return {
        f"{package.name}:{OUTPUT_ROOTS[True]}/{out['destination']}"
        for package in packages
        for out in package.outputs
        if out["persist"]
    }


def census_problems(packages: list[host_mode.Package], listed: set[str]) -> list[str]:
    """What differs between the persisted outputs and `listed`, [] when they are equal."""
    found = persisted(packages)
    return [f"persists an unlisted output: {o}" for o in sorted(found - listed)] + [
        f"lists an output no package renders: {o}" for o in sorted(listed - found)
    ]


def fixture_package(name: str, *, persist: bool) -> host_mode.Package:
    return host_mode.Package(
        name=name,
        directory=f"hosts/{name}",
        outputs=[{"destination": "etc/app/app.conf", "persist": persist}],
    )


def test_the_census_finds_the_packages_it_must_cover():
    names = {package.name for package in rendered_packages()}
    assert MEMBERS <= names


def test_the_persisted_outputs_are_exactly_the_named_list():
    assert census_problems(rendered_packages(), PERSISTED) == []


def test_an_unlisted_persisted_output_is_flagged_and_named():
    packages = [*rendered_packages(), fixture_package("homelab-fixture", persist=True)]
    assert census_problems(packages, PERSISTED) == [
        "persists an unlisted output: "
        "homelab-fixture:/var/lib/homelab-secrets/etc/app/app.conf"
    ]


def test_a_tmpfs_output_is_not_a_persisted_output():
    packages = [*rendered_packages(), fixture_package("homelab-fixture", persist=False)]
    assert census_problems(packages, PERSISTED) == []


def test_a_listed_member_that_no_package_renders_is_flagged():
    listed = PERSISTED | {"homelab-gone:/var/lib/homelab-secrets/etc/gone.conf"}
    assert census_problems(rendered_packages(), listed) == [
        "lists an output no package renders: "
        "homelab-gone:/var/lib/homelab-secrets/etc/gone.conf"
    ]
