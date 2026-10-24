#!/usr/bin/env python3
"""The container case for dpkg taking over the apt sources and keys the roles wrote (H7.24a).

The `apt-docker`, `apt-azlux` and `apt-github-cli` components ship the files that
`docker_install`, `optimize_pi` and `github_cli` wrote on the hosts, at the same paths. dpkg
overwrites a file no package owns unless the package marks it a conffile, and then it prompts.
This case proves the install needs no answer:

  * it removes the image's diversions of the three sources, which keep every other case off the
    network, so the files land at their real paths;
  * it seeds all six paths with the roles' own text (`ROLE_SOURCES`) and a stale key, as a host
    that ran the roles holds them;
  * it installs the `apt-sources` fixture, a host package that lists all three components, with
    apt and no terminal and no `--force-conf*` option, so a conffile prompt would fail the
    install;
  * it asserts the package owns all six paths, that each holds the rendered bytes, that
    `dpkg --verify` is silent about them, and that none is a recorded conffile.

It does not cover the three upstream hosts, an arm64 host, or daniel-server's hand-installed
`github-cli.list`, which reconcile retires and no case here installs.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import hashlib
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from validate.validate_lib.host_container import CaseFailed, Container, expect
from validate.validate_lib.host_fixtures import HOST_PKG, build, render_fixture

NAME = "install the apt-source components over the files the roles wrote"
# The text `ansible.builtin.deb822_repository` wrote for each source, with the lines the
# components' own copies leave out.
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
KEYS = (
    "/etc/apt/keyrings/docker.asc",
    "/etc/apt/keyrings/githubcli-archive-keyring.gpg",
    "/usr/share/keyrings/azlux-archive-keyring.gpg",
)
STALE_KEY = "a key a role fetched before the package existed\n"


def takeover_problems(
    expected: dict[str, str], owners: str, verify: str, conffiles: str, digests: str
) -> list[str]:
    """What the install left wrong, from the container's own output.

    Args:
        expected: Each path the package ships, mapped to the sha256 of the bytes it ships.
        owners: `dpkg -S` output for those paths, naming `HOST_PKG`.
        verify: `dpkg --verify` output for the package.
        conffiles: The package's recorded conffiles, one per line.
        digests: `sha256sum` output for those paths.
    """
    problems = [
        f"dpkg -S does not name {HOST_PKG} for {path}"
        for path in expected
        if f"{HOST_PKG}: {path}" not in owners.splitlines()
    ]
    held = {line.split()[1]: line.split()[0] for line in digests.splitlines() if line}
    problems += [
        f"{path} holds {held.get(path, 'nothing')[:12]}, not the package's {digest[:12]}"
        for path, digest in expected.items()
        if held.get(path) != digest
    ]
    # The fixture's other files are not this case's subject, so only lines about its paths count.
    flagged = [
        line
        for line in verify.splitlines()
        if line.split()[-1:] and line.split()[-1] in expected
    ]
    if flagged:
        problems.append(f"dpkg --verify printed {flagged}")
    kept = [path for path in expected if path in conffiles.split()]
    if kept:
        problems.append(f"{kept} are conffiles, so dpkg prompts over an existing copy")
    return problems


def _body(cases, c: Container) -> None:
    build_root = cases.work / "build"
    rendered = render_fixture("apt-sources", build_root / "apt-sources")
    expected = {
        "/" + str(path.relative_to(rendered / "root")): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in (rendered / "root").rglob("*")
        if path.is_file()
        and (str(path).endswith(".sources") or "keyrings" in str(path))
    }
    expect(
        set(expected) == {*ROLE_SOURCES, *KEYS},
        f"the fixture ships {sorted(expected)}, not the six apt paths",
    )
    version = f"{cases.versions['updater-head']}.7"
    deb = build(rendered, version, build_root / "apt-sources-deb")
    shutil.copy(deb, cases.work / "debs" / deb.name)
    updater = cases.debs["updater-head"].name
    for path in ROLE_SOURCES:
        c.ok(f"dpkg-divert --local --no-rename --remove {path}")
    for path in expected:
        text = ROLE_SOURCES.get(path, STALE_KEY)
        c.ok(
            f"mkdir -p {shlex.quote(str(Path(path).parent))} && printf %s "
            f"{shlex.quote(text)} > {path} && chmod 0644 {path}"
        )
    # No terminal, no --force-conf*: a conffile prompt reads end-of-file and fails the install.
    c.ok(
        "DEBIAN_FRONTEND=noninteractive apt-get install -y"
        f" /srv/debs/{updater} /srv/debs/{deb.name} < /dev/null"
    )
    paths = " ".join(expected)
    problems = takeover_problems(
        expected,
        c.sh(f"dpkg -S {paths}").stdout,
        c.sh(f"dpkg --verify {HOST_PKG}").stdout,
        c.ok(f"dpkg-query -W -f '${{Conffiles}}\\n' {HOST_PKG}"),
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
