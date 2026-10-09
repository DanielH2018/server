"""`lib/renovate_annotations.py` against fixture defaults, with renovate.json's own matchString.

The fixtures are fed to the annotation manager the repo actually ships, so a test here fails
when the regex in renovate.json stops reading an annotation shape, not only when the expander
does.

Run: uv run pytest scripts/lib/tests/test_renovate_annotations.py
"""

import json

from lib.renovate_annotations import (
    annotations_in,
    effective_managers,
    expand,
    is_annotation_manager,
)
from lib.repo_paths import REPO

_CONFIG = json.loads((REPO / "renovate.json").read_text())
_ANNOTATION_MANAGERS = [
    m for m in _CONFIG["customManagers"] if is_annotation_manager(m)
]

DEFAULTS = "ansible/roles/setup/tool/defaults/main.yml"

FIXTURE = """\
---
# renovate: datasource=github-releases depName=vendor/tool
tool_version: "v1.2.3"
tool_url: "https://host/{{ tool_version }}/tool"
# renovate: datasource=deb depName=pkg versioning=semver extractVersion=^(?:\\d+:)?(?<version>[\\d.]+)- registryUrl=https://apt.invalid/ubuntu?suite=noble
pkg_version: 2.0.1
"""


def test_renovate_json_ships_exactly_one_annotation_manager():
    assert len(_ANNOTATION_MANAGERS) == 1, [
        m["description"][:60] for m in _ANNOTATION_MANAGERS
    ]


def test_an_annotation_names_the_pinned_variable_and_its_groups():
    found = annotations_in(_ANNOTATION_MANAGERS[0], FIXTURE)
    assert found == [
        {
            "datasource": "github-releases",
            "depName": "vendor/tool",
            "currentValue": "v1.2.3",
            "variable": "tool_version",
        },
        {
            "datasource": "deb",
            "depName": "pkg",
            "versioning": "semver",
            "extractVersion": "^(?:\\d+:)?(?<version>[\\d.]+)-",
            "registryUrl": "https://apt.invalid/ubuntu?suite=noble",
            "currentValue": "2.0.1",
            "variable": "pkg_version",
        },
    ]


def test_an_annotation_with_its_keys_out_of_order_is_not_read():
    text = (
        "# renovate: depName=vendor/tool datasource=github-releases\n"
        'tool_version: "v1.2.3"\n'
    )
    assert annotations_in(_ANNOTATION_MANAGERS[0], text) == []


def test_an_annotation_separated_from_its_pin_is_not_read():
    text = (
        "# renovate: datasource=github-releases depName=vendor/tool\n"
        "\n"
        'tool_version: "v1.2.3"\n'
    )
    assert annotations_in(_ANNOTATION_MANAGERS[0], text) == []


def test_expand_builds_a_per_pin_manager_scoped_to_the_annotated_line():
    pins = expand(_ANNOTATION_MANAGERS[0], [DEFAULTS, "README.md"], lambda _: FIXTURE)
    deb = pins[1]
    assert [p["annotation"]["variable"] for p in pins] == [
        "tool_version",
        "pkg_version",
    ]
    assert deb["managerFilePatterns"] == [
        "/^ansible/roles/setup/tool/defaults/main\\.yml$/"
    ]
    assert deb["matchStrings"] == ['pkg_version:\\s*"?(?<currentValue>[^"\\s]+)"?']
    assert (deb["depNameTemplate"], deb["datasourceTemplate"]) == ("pkg", "deb")
    assert deb["versioningTemplate"] == "semver"
    assert deb["registryUrlTemplate"] == "https://apt.invalid/ubuntu?suite=noble"
    assert deb["depTypeTemplate"] == "annotated-pin"
    assert "versioningTemplate" not in pins[0], (
        "an annotation with no versioning= must fall back to the datasource default"
    )


def test_effective_managers_replaces_only_the_annotation_reader():
    other = {"managerFilePatterns": ["/x/"], "matchStrings": ["x"]}
    managers = effective_managers(
        [other, _ANNOTATION_MANAGERS[0]], [DEFAULTS], lambda _: FIXTURE
    )
    assert managers[0] is other
    assert [m["depNameTemplate"] for m in managers[1:]] == ["vendor/tool", "pkg"]
