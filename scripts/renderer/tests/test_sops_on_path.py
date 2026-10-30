"""Every host package puts the packaged `sops` on PATH and retires sops_setup's copy (H7.24b).

docs/target-architecture/host/h7-delete-ansible.md, item 4. homelab-tools installs `sops` off
PATH, and callers such as `deploy.sh`'s community.sops lookups and the rotation cron run a bare
`sops`. Each host package therefore ships `/usr/bin/sops`, an exec of the packaged binary, and
lists `/usr/local/bin/sops` as a legacy entry, because that directory comes first on PATH.
"""

from renderer.renderer_lib import host as host_mode
from renderer.renderer_lib.host_package import Package

HOSTS = ("daniel-box", "daniel-pi", "daniel-server")
ON_PATH = "/usr/bin/sops"
PACKAGED = "/usr/lib/homelab/bin/sops"
LEGACY = "/usr/local/bin/sops"


def sops_problems(package: Package) -> list[str]:
    """Each way `package` leaves a bare `sops` resolving somewhere other than the packaged binary."""
    problems = []
    entry = package.files.get(ON_PATH)
    if entry is None:
        problems.append(f"{package.name} ships no {ON_PATH}")
    else:
        text, mode = entry
        if f'exec {PACKAGED} "$@"' not in text.decode():
            problems.append(f"{ON_PATH} does not exec {PACKAGED}")
        if mode & 0o111 != 0o111:
            problems.append(f"{ON_PATH} is not executable")
    if {"path": LEGACY} not in package.legacy:
        problems.append(f"{package.name} does not retire {LEGACY}")
    problems += [
        f"{package.name} does not depend on {dep}"
        for dep in ("homelab-tools", "age")
        if dep not in package.depends
    ]
    return problems


def test_every_real_host_package_puts_the_packaged_sops_on_path():
    for host in HOSTS:
        assert sops_problems(host_mode.render_host(host)) == [], host


def test_a_package_that_keeps_the_old_binary_or_ships_a_wrong_wrapper_is_flagged():
    package = host_mode.render_host("daniel-box")
    package.legacy = [e for e in package.legacy if e != {"path": LEGACY}]
    package.files[ON_PATH] = (b'#!/bin/sh\nexec /usr/local/bin/sops "$@"\n', 0o644)
    package.depends = [d for d in package.depends if d != "age"]
    assert sops_problems(package) == [
        f"{ON_PATH} does not exec {PACKAGED}",
        f"{ON_PATH} is not executable",
        f"homelab-daniel-box does not retire {LEGACY}",
        "homelab-daniel-box does not depend on age",
    ]


def test_a_package_without_the_wrapper_is_flagged():
    package = host_mode.render_host("daniel-pi")
    del package.files[ON_PATH]
    assert sops_problems(package) == [f"homelab-daniel-pi ships no {ON_PATH}"]
