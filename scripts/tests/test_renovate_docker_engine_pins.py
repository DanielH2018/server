#!/usr/bin/env python3
"""Guard that the Pi's four Docker engine pins track the apt index they install from.

`hosts/components/pi-docker/templates/docker-engine.pins.j2` renders each pin into a glob
(`containerd.io 2.3.5-*~ubuntu.24.04~noble`), so the version space that matters is Docker's
own apt index, not the upstream GitHub tags. The managers read the apt index, not
github-releases, because Docker packages a release days-to-weeks after upstream tags it: a
bare upstream tag can name a version the noble/arm64 index does not carry, and
homelab-docker-engine.service would then refuse the upgrade. No CI job resolves the rendered spec.

The guards here are offline by construction — the operator rejected a CI job that fetches
Docker's index on every run. They assert the config's own internal agreement instead: the
depName is the apt package its var renders into, the registryUrl is Docker's index with the
three query parameters the `deb` datasource requires, extractVersion reads the apt version
shapes that index actually serves. The `docker-engine-` rows of
test_renovate_package_rules.py keep the group out of the automerging catch-all.

Run: uv run pytest scripts/tests/test_renovate_docker_engine_pins.py
"""

import re
from urllib.parse import parse_qs, urlparse

import pytest

from _renovate import _CONFIG_MANAGERS, _REPO
from lib.renovate_annotations import (
    annotations_in,
    is_annotation_manager,
    to_python_regex,
)


# Named rather than derived from renovate.json, so a manager that lost its depName fails here
# instead of shrinking the census to nothing and passing.
PI_DOCKER_DEB_PACKAGES = frozenset(
    {"docker-ce", "containerd.io", "docker-compose-plugin", "docker-buildx-plugin"}
)

PI_DOCKER_COMPONENT = "hosts/components/pi-docker/component.yml"
PINS_TEMPLATE = "hosts/components/pi-docker/templates/docker-engine.pins.j2"

# The apt version strings the index serves
# (https://download.docker.com/linux/ubuntu/dists/noble/stable/binary-arm64/Packages):
# containerd.io carries bare `<version>-<rev>` rows beside the suffixed ones, and docker-ce a `5:`
# epoch. A version extractVersion cannot match is dropped from the version space, so a regex
# anchored on `~ubuntu` would silently narrow it.
_APT_VERSION_FIXTURES = [
    ("5:29.8.1-1~ubuntu.24.04~noble", "29.8.1"),
    ("2.3.5-1~ubuntu.24.04~noble", "2.3.5"),
    ("2.3.4-2~ubuntu.24.04~noble", "2.3.4"),
    ("1.6.9-1", "1.6.9"),
    ("0.37.1-1~ubuntu.24.04~noble", "0.37.1"),
]

# Shapes extractVersion must NOT turn into a version. A bare upstream tag is the one that
# matters: it is what a github-releases datasource would hand over, and it is exactly the
# version apt has no package for.
_NON_APT_VERSION_FIXTURES = ["2.4.0", "v2.4.0", "29.8.1"]


def _pi_docker_pins() -> list[dict[str, str]]:
    """Each `# renovate:` annotation in the component, as the annotation manager reads it."""
    (manager,) = [m for m in _CONFIG_MANAGERS if is_annotation_manager(m)]
    return annotations_in(manager, (_REPO / PI_DOCKER_COMPONENT).read_text())


def test_each_annotation_names_the_apt_package_its_var_renders() -> None:
    """Each pin's depName must be the apt package whose spec its var renders.

    The bug guarded is a pin tracking a version space its own consumer cannot install. The
    pins template is that consumer: each row names an apt package and interpolates the
    pinned var, so binding depName to the row's package is what makes the datasource and the
    glob name the same thing.
    """
    rows = (_REPO / PINS_TEMPLATE).read_text().splitlines()
    package_versions = {
        row.split()[0]: row.split(maxsplit=1)[1]
        for row in rows
        if row and not row.startswith(("#", "{"))
    }
    assert {"docker-ce", "containerd.io"} <= set(package_versions), package_versions

    pins = _pi_docker_pins()
    assert {p["depName"] for p in pins} == PI_DOCKER_DEB_PACKAGES, (
        "the pi-docker annotations no longer cover exactly the apt packages the Pi holds — "
        f"got {sorted(p['depName'] for p in pins)}"
    )
    for pin in pins:
        owners = {k for k, v in package_versions.items() if pin["variable"] in v}
        assert pin["depName"] in owners, (
            f"{pin['depName']} is tracked against {pin['variable']}, but the pins template "
            f"renders that var into {sorted(owners)} — the Renovate depName and the apt spec "
            "name different packages."
        )


def test_each_annotation_reads_dockers_own_apt_index() -> None:
    """Every one of the four resolves against Docker's noble/arm64 index, with all three
    query parameters the deb datasource requires. Omit one and the datasource is inert."""
    pins = _pi_docker_pins()
    assert len(pins) == len(PI_DOCKER_DEB_PACKAGES)
    for pin in pins:
        assert pin["datasource"] == "deb", pin["depName"]
        url = urlparse(pin["registryUrl"])
        assert (url.netloc, url.path) == ("download.docker.com", "/linux/ubuntu"), (
            f"{pin['depName']} resolves against {pin['registryUrl']!r}, not "
            "the repo apt actually installs from."
        )
        params = parse_qs(url.query)
        assert params.get("suite") == ["noble"], params
        assert params.get("components") == ["stable"], params
        assert params.get("binaryArch") == ["arm64"], params


@pytest.mark.parametrize(("apt_version", "expected"), _APT_VERSION_FIXTURES)
def test_extract_version_reads_every_apt_shape(apt_version: str, expected: str) -> None:
    for pin in _pi_docker_pins():
        match = re.search(to_python_regex(pin["extractVersion"]), apt_version)
        assert match is not None, (
            f"{pin['depName']}'s extractVersion drops {apt_version!r} from the version "
            "space — Renovate cannot offer a version it cannot parse."
        )
        assert match.group("version") == expected


@pytest.mark.parametrize("not_an_apt_version", _NON_APT_VERSION_FIXTURES)
def test_extract_version_rejects_a_bare_upstream_tag(not_an_apt_version: str) -> None:
    """The rejecting half: a bare upstream tag is what github-releases would serve, and it
    is exactly the version the Pi's apt has no package for."""
    for pin in _pi_docker_pins():
        assert (
            re.search(to_python_regex(pin["extractVersion"]), not_an_apt_version)
            is None
        ), (
            f"{pin['depName']}'s extractVersion accepts {not_an_apt_version!r}, a tag with "
            "no Debian revision — the unbuildable version space #2341 closed."
        )
