#!/usr/bin/env python3
"""Renovate resolves each k8s image pin that `setup/k8s_host_files` copies like its source.

`setup/k8s_host_files` keeps copies of the registry role's image pins for the ghcr-mirror GC
Job (#5827), and `ansible/tests/setup/test_k8s_host_files_pairs.py` holds each copy equal to
its source. Renovate updates both copies, but it puts a dep on a branch by the group name the
packageRules resolve for that file. When the two files resolve differently, each copy lands in
its own PR and both PRs fail the pairs test. #5859 and #5835 did that: the copy matched none of
the k8s-plane rules, so it got a bare digest branch with no soak, while its source sat in the
`k8s image registry (manual — …)` group waiting out a 3-day soak.

So for every copied `_image` pin and every update type, the settings that decide the branch,
the soak and the merge must resolve the same for the copy's path as for its source's.
`_resolve_setting` walks the rules in order the way Renovate does.

Run: uv run pytest scripts/tests/test_renovate_host_file_copies.py
"""

from pathlib import Path
from typing import NamedTuple

import pytest
from _renovate import _PACKAGE_RULES, _REPO, _resolve_setting
from lib import yaml_fast

HOST_FILES_DEFAULTS = _REPO / "ansible/roles/setup/k8s_host_files/defaults/main.yml"
K8S_ROLES = _REPO / "ansible/roles/k8s"

# The copies the census must find, so a moved or renamed pin fails as a missing member rather
# than passing over an empty census.
KNOWN_COPIES = frozenset({"registry_k8s_image", "registry_k8s_netpol_probe_image"})

FIELDS = (
    "groupName",
    "groupSlug",
    "groupSingleUpdates",
    "automerge",
    "minimumReleaseAge",
    "minimumReleaseAgeBehaviour",
)
UPDATE_TYPES = ("major", "minor", "patch", "digest", "pinDigest")


class Copy(NamedTuple):
    """One `_image` key the host-files defaults share with a k8s role's defaults.

    Attributes:
      key: The variable name both files define.
      dep_name: The image name Renovate reads from the pin.
      source: The k8s role's defaults path, repo-relative.
      copy: The host-files defaults path, repo-relative.
    """

    key: str
    dep_name: str
    source: str
    copy: str


def image_name(ref: str) -> str:
    """The image name in `ref`, without its tag or digest: `registry:3.1.2@sha256:…` → `registry`."""
    name = ref.split("@", 1)[0]
    head, _, last = name.rpartition("/")
    return f"{head}/{last.split(':', 1)[0]}" if head else last.split(":", 1)[0]


def copied_pins(
    copies: Path = HOST_FILES_DEFAULTS, k8s_roles: Path = K8S_ROLES
) -> list[Copy]:
    """Every `_image` key in `copies` that some `roles/k8s/*/defaults/main.yml` also defines."""
    pins = {
        k: v
        for k, v in (yaml_fast.safe_load(copies.read_text()) or {}).items()
        if str(k).endswith("_image")
    }
    found = []
    for defaults in sorted(k8s_roles.glob("*/defaults/main.yml")):
        values = yaml_fast.safe_load(defaults.read_text()) or {}
        for key in sorted(pins.keys() & values.keys()):
            found.append(
                Copy(
                    key,
                    image_name(str(values[key])),
                    defaults.relative_to(_REPO).as_posix(),
                    copies.relative_to(_REPO).as_posix(),
                )
            )
    return found


def resolution_mismatches(
    pins: list[Copy], rules: list[dict] = _PACKAGE_RULES
) -> list[str]:
    """Each field and update type where a copy resolves differently from its source."""
    problems = []
    for pin in pins:
        for update_type in UPDATE_TYPES:
            for field in FIELDS:
                at_source = _resolve_setting(
                    field, pin.dep_name, pin.source, update_type, "docker", rules
                )
                at_copy = _resolve_setting(
                    field, pin.dep_name, pin.copy, update_type, "docker", rules
                )
                if at_source != at_copy:
                    problems.append(
                        f"{pin.key} ({pin.dep_name}, {update_type}): {field} is "
                        f"{at_source!r} in {pin.source} but {at_copy!r} in {pin.copy}"
                    )
    return problems


def test_the_census_finds_the_registry_copies() -> None:
    found = {pin.key for pin in copied_pins()}
    assert KNOWN_COPIES <= found, sorted(KNOWN_COPIES - found)


def test_every_copy_resolves_like_its_source() -> None:
    problems = resolution_mismatches(copied_pins())
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize(
    ("ref", "name"),
    [
        ("registry:3.1.2@sha256:ab", "registry"),
        ("alpine:3.24", "alpine"),
        ("ghcr.io/koenkk/zigbee2mqtt:2.1@sha256:ab", "ghcr.io/koenkk/zigbee2mqtt"),
        ("host:5000/team/app:1", "host:5000/team/app"),
    ],
)
def test_image_name_strips_the_tag_and_digest(ref: str, name: str) -> None:
    assert image_name(ref) == name


# --- the red-proof pair: a rule that reaches both paths is clean, one that misses the copy is not

_PIN = Copy(
    "x_image",
    "registry",
    "ansible/roles/k8s/registry/defaults/main.yml",
    "ansible/roles/setup/k8s_host_files/defaults/main.yml",
)


def _rules(paths: list[str]) -> list[dict]:
    return [
        {
            "matchFileNames": paths,
            "groupName": "k8s image {{depName}}",
            "matchUpdateTypes": ["digest"],
            "minimumReleaseAge": "3 days",
        }
    ]


def test_a_rule_matching_both_copies_is_clean() -> None:
    rules = _rules(["ansible/roles/k8s/**", _PIN.copy])
    assert resolution_mismatches([_PIN], rules) == []


def test_a_rule_missing_the_copy_is_flagged() -> None:
    problems = resolution_mismatches([_PIN], _rules(["ansible/roles/k8s/**"]))
    assert problems == [
        "x_image (registry, digest): groupName is 'k8s image {{depName}}' in "
        f"{_PIN.source} but None in {_PIN.copy}",
        "x_image (registry, digest): minimumReleaseAge is '3 days' in "
        f"{_PIN.source} but None in {_PIN.copy}",
    ]
