"""Each host package's tmpfiles.d lines hold on a fresh host, where bootstrap step 5 runs them.

Step 5 of `hosts/bootstrap-host.sh` runs `systemd-sysusers`, then `systemd-tmpfiles --create`,
straight after the install. A line naming a user the package does not declare fails there, and
a missing parent under the operator's home is created root-owned, which tmpfiles then refuses as
an unsafe path transition (#8088).

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


def tmpfiles_lines(package: host_mode.Package) -> list[list[str]]:
    return [
        line.split()
        for dst, (body, *_) in package.files.items()
        if dst.startswith(("/usr/lib/tmpfiles.d/", "/etc/tmpfiles.d/"))
        for line in body.decode().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


@pytest.fixture(scope="module", params=HOSTS)
def package(request) -> host_mode.Package:
    return host_mode.render_host(request.param)


def test_every_user_a_tmpfiles_line_names_exists_once_sysusers_has_run(package):
    sysusers = package.files.get("/usr/lib/sysusers.d/homelab.conf")
    declared = {
        line.split()[1]
        for line in (sysusers[0].decode() if sysusers else "").splitlines()
        if line.startswith("u ")
    }
    named = {
        owner.lstrip(":")
        for fields in tmpfiles_lines(package)
        for owner in fields[3:5]
        if owner != "-" and not owner.lstrip(":").isdigit()
    }
    assert named - BASE - declared == set()


def test_every_parent_under_the_operators_home_is_declared_and_owned_by_the_operator(
    package,
):
    owners = {
        PurePosixPath(fields[1]): fields[3].lstrip(":")
        for fields in tmpfiles_lines(package)
        if len(fields) > 3
    }
    undeclared = {
        str(parent)
        for path in owners
        if HOME in path.parents
        for parent in path.parents
        if HOME in parent.parents and owners.get(parent) != SYS_USER
    }
    assert undeclared == set()
