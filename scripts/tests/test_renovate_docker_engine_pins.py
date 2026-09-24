#!/usr/bin/env python3
"""Guard that the Pi's four Docker engine pins track the apt index they install from.

`docker_install_package_specs` renders each pin into an apt spec
(`containerd.io=2.3.5-*~ubuntu.24.04~noble`), so the version space that matters is Docker's
own apt index, not the upstream GitHub tags. Until #2341 the managers read github-releases,
and Docker packages a release days-to-weeks after upstream tags it: PR #2327 offered
containerd 2.4.0 while the noble/arm64 index topped out at 2.3.5, so the
`docker-engine-upgrade` play would have failed at apt AFTER stopping every Compose project
on the Pi. Nothing in CI said so, because no job resolved the rendered spec.

The guards here are offline by construction — the operator rejected a CI job that fetches
Docker's index on every run. They assert the config's own internal agreement instead: the
depName is the apt package its var renders into, the registryUrl is Docker's index with the
three query parameters the `deb` datasource requires, extractVersion reads the apt version
shapes that index actually serves, and the group stays out of the automerging catch-all.

Run: uv run pytest scripts/tests/test_renovate_docker_engine_pins.py
"""

import re
from urllib.parse import parse_qs, urlparse

import pytest
from lib import yaml_fast

from _renovate import (
    _MANAGERS,
    _PACKAGE_RULES,
    _REPO,
    _resolve_group_name,
    _to_python_regex,
)


# Named rather than derived from renovate.json, so a manager that lost its depName fails here
# instead of shrinking the census to nothing and passing.
PI_DOCKER_DEB_PACKAGES = frozenset(
    {"docker-ce", "containerd.io", "docker-compose-plugin", "docker-buildx-plugin"}
)

DOCKER_INSTALL_DEFAULTS = "ansible/roles/setup/docker_install/defaults/main.yml"

_PI_DOCKER_GROUP_PREFIX = "docker engine on the Pi"

# The apt version strings the index actually serves, read from
# https://download.docker.com/linux/ubuntu/dists/noble/stable/binary-arm64/Packages on
# 2026-09-24. Both shapes are present: containerd.io carries bare `<version>-<rev>` rows
# alongside the suffixed ones, and docker-ce carries a `5:` epoch. A version extractVersion
# cannot match is dropped from the version space, so a regex anchored on `~ubuntu` would
# silently narrow it.
_APT_VERSION_FIXTURES = [
    ("5:29.8.1-1~ubuntu.24.04~noble", "29.8.1"),
    ("2.3.5-1~ubuntu.24.04~noble", "2.3.5"),
    ("2.3.4-2~ubuntu.24.04~noble", "2.3.4"),
    ("1.6.9-1", "1.6.9"),
    ("0.37.1-1~ubuntu.24.04~noble", "0.37.1"),
]

# Shapes extractVersion must NOT turn into a version. A bare upstream tag is the one that
# matters: it is what the github-releases datasource used to hand over, and it is exactly the
# version apt has no package for.
_NON_APT_VERSION_FIXTURES = ["2.4.0", "v2.4.0", "29.8.1"]


def _pi_docker_managers() -> list[dict]:
    return [
        m
        for m in _MANAGERS
        if any(
            "docker_install/defaults" in p.replace("\\", "")
            for p in m["managerFilePatterns"]
        )
    ]


def test_managers_name_the_apt_packages_they_pin() -> None:
    """Each manager's depName must be the apt package whose spec its var renders.

    The bug #2341 records is a manager tracking a version space its own consumer cannot
    install. `docker_install_package_versions` is that consumer: its keys are the apt package
    names and its values interpolate the pinned vars, so binding depName to the key is what
    makes the datasource and the spec name the same thing.
    """
    # fact: ansible/roles/setup/docker_install/CLAUDE.md#The engine is held; `--tags docker-engine-upgrade` is how it moves
    defaults = yaml_fast.safe_load((_REPO / DOCKER_INSTALL_DEFAULTS).read_text())
    package_versions = defaults["docker_install_package_versions"]

    managers = _pi_docker_managers()
    assert {m["depNameTemplate"] for m in managers} == PI_DOCKER_DEB_PACKAGES, (
        "the docker_install managers no longer cover exactly the apt packages the Pi holds — "
        f"got {sorted(m['depNameTemplate'] for m in managers)}"
    )

    for mgr in managers:
        var = re.match(r"(\w+):", mgr["matchStrings"][0]).group(1)
        owners = {k for k, v in package_versions.items() if var in v}
        assert mgr["depNameTemplate"] in owners, (
            f"{mgr['depNameTemplate']} is tracked against {var}, but "
            f"docker_install_package_versions renders that var into {sorted(owners)} — the "
            "Renovate depName and the apt spec name different packages."
        )


def test_managers_read_dockers_own_apt_index() -> None:
    """Every one of the four resolves against Docker's noble/arm64 index, with all three
    query parameters the deb datasource requires. Omit one and the datasource is inert."""
    managers = _pi_docker_managers()
    assert len(managers) == len(PI_DOCKER_DEB_PACKAGES)
    for mgr in managers:
        assert mgr["datasourceTemplate"] == "deb", mgr["depNameTemplate"]
        url = urlparse(mgr["registryUrlTemplate"])
        assert (url.netloc, url.path) == ("download.docker.com", "/linux/ubuntu"), (
            f"{mgr['depNameTemplate']} resolves against {mgr['registryUrlTemplate']!r}, not "
            "the repo apt actually installs from."
        )
        params = parse_qs(url.query)
        assert params.get("suite") == ["noble"], params
        assert params.get("components") == ["stable"], params
        assert params.get("binaryArch") == ["arm64"], params


@pytest.mark.parametrize(("apt_version", "expected"), _APT_VERSION_FIXTURES)
def test_extract_version_reads_every_apt_shape(apt_version: str, expected: str) -> None:
    for mgr in _pi_docker_managers():
        pattern = re.compile(_to_python_regex(mgr["extractVersionTemplate"]))
        match = pattern.search(apt_version)
        assert match is not None, (
            f"{mgr['depNameTemplate']}'s extractVersionTemplate drops {apt_version!r} from the "
            "version space — Renovate cannot offer a version it cannot parse."
        )
        assert match.group("version") == expected


@pytest.mark.parametrize("not_an_apt_version", _NON_APT_VERSION_FIXTURES)
def test_extract_version_rejects_a_bare_upstream_tag(not_an_apt_version: str) -> None:
    """The rejecting half: a bare upstream tag is what github-releases used to serve, and it
    is exactly the version the Pi's apt has no package for."""
    for mgr in _pi_docker_managers():
        pattern = re.compile(_to_python_regex(mgr["extractVersionTemplate"]))
        assert pattern.search(not_an_apt_version) is None, (
            f"{mgr['depNameTemplate']}'s extractVersionTemplate accepts {not_an_apt_version!r}, "
            "a tag with no Debian revision — the unbuildable version space #2341 closed."
        )


@pytest.mark.parametrize("dep_name", sorted(PI_DOCKER_DEB_PACKAGES))
def test_pins_resolve_to_the_manual_group(dep_name: str) -> None:
    """A minor bump must land in the Pi's own manual group, never the automerging catch-all.

    `container images (non-major)` has no matchFileNames and automerges minor and patch. The
    override that keeps these four out of it selects on datasource AND package name, so the
    #2341 datasource move had to carry it — a rule left on github-releases/moby-moby would
    have matched nothing and started automerging engine bumps onto a Pi that cannot apply
    them without the docker-engine-upgrade play.
    """
    group = _resolve_group_name(dep_name, DOCKER_INSTALL_DEFAULTS, "minor", "deb")
    assert group is not None and group.startswith(_PI_DOCKER_GROUP_PREFIX), (
        f"{dep_name} resolves to {group!r}"
    )

    rule = next(
        r
        for r in _PACKAGE_RULES
        if r.get("groupName", "").startswith(_PI_DOCKER_GROUP_PREFIX)
    )
    assert rule["automerge"] is False


def test_group_resolution_catches_the_catch_all() -> None:
    """The red proof for the guard above: without the override, the catch-all wins."""
    fake_rules = [
        {
            "matchManagers": ["custom.regex"],
            "matchUpdateTypes": ["minor", "patch"],
            "groupName": "container images (non-major)",
        }
    ]
    assert (
        _resolve_group_name(
            "containerd.io", DOCKER_INSTALL_DEFAULTS, "minor", "deb", fake_rules
        )
        == "container images (non-major)"
    )
