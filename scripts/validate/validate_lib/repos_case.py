#!/usr/bin/env python3
"""The container case for dpkg taking over the apt sources and keys the roles wrote (H7.24a).

`homelab-repos` ships the files that `docker_install`, `github_cli` and `optimize_pi` wrote on
the hosts, at the same paths. dpkg overwrites a file no package owns unless the package marks it
a conffile, and then it prompts. This case proves the install needs no answer:

  * it removes the image's diversions of the three sources, which keep every other case off the
    network, so the files land at their real paths;
  * it seeds all six paths with the roles' own text (`ROLE_SOURCES`) and a stale key, as a host
    that ran the roles holds them;
  * it installs the real `homelab-repos` build with `dpkg -i`, with no terminal and no
    `--force-conf*` option, so a conffile prompt would fail the install;
  * it asserts the package owns all six paths, that each holds the package's bytes, that
    `dpkg --verify` is silent, and that the package lists no conffile.

It does not cover the three upstream hosts, an arm64 host, or daniel-server's hand-installed
`github-cli.list`, which reconcile retires and no case here installs.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import hashlib
import shlex
import subprocess
import sys

from renderer.renderer_lib import host as host_mode
from validate.validate_lib.host_container import CaseFailed, Container, expect

PACKAGE = "homelab-repos"
NAME = "install homelab-repos over the files the roles wrote"
# The text `ansible.builtin.deb822_repository` wrote for each source, with the lines the
# package's own copy leaves out.
ROLE_SOURCES = {
    "/etc/apt/sources.list.d/docker.sources": (
        "Architectures: arm64\nComponents: stable\nX-Repolib-Name: docker\n"
        "Signed-By: /etc/apt/keyrings/docker.asc\nSuites: noble\nTypes: deb\n"
        "URIs: https://download.docker.com/linux/ubuntu\n"
    ),
    "/etc/apt/sources.list.d/github-cli.sources": (
        "Architectures: arm64\nComponents: main\nX-Repolib-Name: github-cli\n"
        "Signed-By: /etc/apt/keyrings/githubcli-archive-keyring.gpg\nSuites: stable\n"
        "Types: deb\nURIs: https://cli.github.com/packages\n"
    ),
    "/etc/apt/sources.list.d/azlux.sources": (
        "Components: main\nX-Repolib-Name: azlux\n"
        "Signed-By: /usr/share/keyrings/azlux-archive-keyring.gpg\nSuites: bookworm\n"
        "Types: deb\nURIs: http://packages.azlux.fr/debian/\n"
    ),
}
STALE_KEY = "a key a role fetched before the package existed\n"


def takeover_problems(
    expected: dict[str, str], owners: str, verify: str, control: str, digests: str
) -> list[str]:
    """What the install left wrong, from the container's own output.

    Args:
        expected: Each path the package ships, mapped to the sha256 of the bytes it ships.
        owners: `dpkg -S` output for those paths.
        verify: `dpkg --verify homelab-repos` output.
        control: The member names of the deb's control archive.
        digests: `sha256sum` output for those paths.
    """
    problems = [
        f"dpkg -S does not name {PACKAGE} for {path}"
        for path in expected
        if f"{PACKAGE}: {path}" not in owners.splitlines()
    ]
    held = {line.split()[1]: line.split()[0] for line in digests.splitlines() if line}
    problems += [
        f"{path} holds {held.get(path, 'nothing')[:12]}, not the package's {digest[:12]}"
        for path, digest in expected.items()
        if held.get(path) != digest
    ]
    if verify.strip():
        problems.append(f"dpkg --verify printed {verify.strip()!r}")
    if any(member.endswith("conffiles") for member in control.splitlines()):
        problems.append(
            "the deb lists conffiles, so dpkg prompts over an existing copy"
        )
    return problems


def _body(cases, c: Container) -> None:
    files = host_mode.render_package(PACKAGE).files
    expected = {
        path: hashlib.sha256(data).hexdigest() for path, (data, _) in files.items()
    }
    name = cases.debs["repos-head"].name
    for path in ROLE_SOURCES:
        c.ok(f"dpkg-divert --local --no-rename --remove {path}")
    for path in expected:
        text = ROLE_SOURCES.get(path, STALE_KEY)
        c.ok(
            f"mkdir -p {shlex.quote(str(_Path(path).parent))} && printf %s "
            f"{shlex.quote(text)} > {path} && chmod 0644 {path}"
        )
    # No terminal, no --force-conf*: a conffile prompt reads end-of-file and fails the install.
    c.ok(f"DEBIAN_FRONTEND=noninteractive dpkg -i /srv/debs/{name} < /dev/null")
    paths = " ".join(expected)
    problems = takeover_problems(
        expected,
        c.sh(f"dpkg -S {paths}").stdout,
        c.sh(f"dpkg --verify {PACKAGE}").stdout,
        c.ok(f"dpkg-deb --ctrl-tarfile /srv/debs/{name} | tar -t"),
        c.ok(f"sha256sum {paths}"),
    )
    expect(not problems, "; ".join(problems))


def run(cases) -> None:
    """Boot a container, run the case, and record the verdict on `cases`."""
    c = cases.boot("repos")
    if c is None:
        return
    try:
        _body(cases, c)
    except (CaseFailed, subprocess.SubprocessError) as exc:
        cases.failed.append(NAME)
        print(f"[FAIL] {c.name}: {NAME}: {exc}", file=sys.stderr)
    else:
        print(f"[PASS] {c.name}: {NAME}", file=sys.stderr)
    finally:
        c.remove()
