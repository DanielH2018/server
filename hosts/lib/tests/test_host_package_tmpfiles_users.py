"""Each host package's tmpfiles.d lines hold on a fresh host, where bootstrap step 5 runs them.

Step 5 of `hosts/bootstrap-host.sh` runs `systemd-sysusers`, then `systemd-tmpfiles --create`,
straight after the install; `test_bootstrap_host.py` holds that order. These tests hold what the
packages declare. A line naming a user no package declares fails at step 5, and a missing parent
under the operator's home is created root-owned, which tmpfiles then refuses as an unsafe path
transition (#8088).

Run: uv run pytest hosts/lib/tests/test_host_package_tmpfiles_users.py
"""

from pathlib import PurePosixPath

import pytest

from renderer.renderer_lib import host as host_mode

HOSTS = ("daniel-box", "daniel-server", "daniel-pi")
SYS_USER = "ubuntu"
# The Ubuntu image's users and groups; the installer creates the operator's.
BASE = {"root", "adm", SYS_USER}
HOME = PurePosixPath("/home") / SYS_USER
SYSUSERS = "/usr/lib/sysusers.d/homelab.conf"

Files = dict[str, tuple[bytes, int]]


def tmpfiles_lines(files: Files) -> list[list[str]]:
    return [
        line.split()
        for dst, (body, _mode) in files.items()
        if dst.startswith(("/usr/lib/tmpfiles.d/", "/etc/tmpfiles.d/"))
        for line in body.decode().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def undeclared_users(files: Files) -> set[str]:
    """The users and groups tmpfiles lines name that neither the image nor sysusers.d creates."""
    sysusers = files.get(SYSUSERS, (b"", 0))[0].decode()
    declared = {
        line.split()[1] for line in sysusers.splitlines() if line.startswith("u ")
    }
    named = {
        owner.lstrip(":")
        for fields in tmpfiles_lines(files)
        for owner in fields[3:5]
        if owner != "-" and not owner.lstrip(":").isdigit()
    }
    return named - BASE - declared


@pytest.fixture(scope="module")
def packages() -> dict[str, Files]:
    return {host: host_mode.render_host(host).files for host in HOSTS}


def test_every_user_a_tmpfiles_line_names_is_declared_in_sysusers_d(packages):
    assert {host: undeclared_users(files) for host, files in packages.items()} == {
        host: set() for host in HOSTS
    }


def test_a_tmpfiles_line_naming_an_undeclared_user_is_reported(packages):
    planted = b"d /var/lib/planted 0700 planted root -\n"
    files = {
        **packages["daniel-box"],
        "/usr/lib/tmpfiles.d/planted.conf": (planted, 0o644),
    }

    assert undeclared_users(files) == {"planted"}


def test_every_parent_under_the_operators_home_is_declared_and_owned_by_the_operator(
    packages,
):
    nested, undeclared = set(), set()
    for host, files in packages.items():
        owners = {
            PurePosixPath(fields[1]): fields[3].lstrip(":")
            for fields in tmpfiles_lines(files)
            if len(fields) > 3
        }
        for path in owners:
            for parent in path.parents:
                if HOME in parent.parents:
                    nested.add((host, str(parent)))
                    if owners.get(parent) != SYS_USER:
                        undeclared.add((host, str(parent)))

    assert {
        ("daniel-box", str(HOME / ".claude")),
        ("daniel-pi", str(HOME / "server")),
    } <= nested
    assert undeclared == set()
