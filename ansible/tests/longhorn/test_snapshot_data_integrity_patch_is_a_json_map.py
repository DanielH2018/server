#!/usr/bin/env python3
"""Guards the shape of the rendered `snapshot-data-integrity` Settings.

`snapshot-data-integrity` is `DataEngineSpecific: true`, which makes its `value` a JSON MAP
SERIALISED AS A STRING -- `{"v1":"enabled","v2":"enabled"}` -- and not the bare string every
other Longhorn setting the `longhorn` role renders takes. The failure mode is quiet: a Setting
copying a sibling's `value: enabled` shape is accepted by the API, stored, and ignored by
Longhorn, so the cluster keeps running `fast-check` while the repo and a green deploy both say
`enabled`. The shape is worth a guard rather than a comment.

The two halves below are the accept/reject pair this repo requires of a new check: a bare-string
value must be REJECTED, and the rendered map must be ACCEPTED. Without the rejecting half there
is no evidence the assertion can go red. The rendered Settings are read from `rendered_docs()`,
the deploy's own render, and `NAMED` below is the member each read must find, so a render that
stops producing a Setting fails by name.

Run: uv run pytest ansible/tests/longhorn/test_snapshot_data_integrity_patch_is_a_json_map.py
"""

import json

import pytest

from _helpers import load_yaml
from _k8s_render import rendered_docs
from lib.repo_paths import K8S_ROLES

LONGHORN_DEFAULTS = K8S_ROLES / "longhorn" / "defaults" / "main.yml"

# Both settings are DataEngineSpecific, so both must carry the map shape.
# Setting name -> the defaults key its value renders from.
MAP_SETTINGS = {
    "snapshot-data-integrity": "k3s_longhorn_snapshot_data_integrity",
    "snapshot-data-integrity-cronjob": "k3s_longhorn_snapshot_data_integrity_cronjob",
}


def _rendered_value(name: str) -> object:
    """The `value` of the Setting `name` in the `longhorn` role's render."""
    for role, _tpl, doc in rendered_docs():
        if (
            role == "longhorn"
            and doc.get("kind") == "Setting"
            and doc["metadata"]["name"] == name
        ):
            return doc["value"]
    raise AssertionError(
        f"the longhorn render holds no Setting {name!r}; it is what arms bit-rot detection"
    )


def _is_map_of(value: object, expected: str) -> bool:
    """True when `value` is a STRING whose content parses as the {v1, v2} map of `expected`."""
    if not isinstance(value, str):
        return False
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return False
    return parsed == {"v1": expected, "v2": expected}


@pytest.mark.parametrize("name", MAP_SETTINGS)
def test_a_rendered_setting_carries_its_default_as_a_json_map_string(name: str) -> None:
    """The shape Longhorn actually reads, built from the value the defaults hold."""
    default = load_yaml(LONGHORN_DEFAULTS)[MAP_SETTINGS[name]]
    assert _is_map_of(_rendered_value(name), default), (
        f"{name} must render as a string holding {{'v1': {default!r}, 'v2': {default!r}}}; "
        f"got {_rendered_value(name)!r}"
    )


def test_a_bare_string_value_is_flagged() -> None:
    """The mistake this guard exists for: copying a sibling setting's `value: enabled`."""
    assert _is_map_of('{"v1":"enabled","v2":"enabled"}', "enabled")
    assert not _is_map_of("enabled", "enabled")
    assert not _is_map_of({"v1": "enabled", "v2": "enabled"}, "enabled")


def test_the_mode_is_a_value_longhorn_knows() -> None:
    defaults = load_yaml(LONGHORN_DEFAULTS)
    assert defaults["k3s_longhorn_snapshot_data_integrity"] in (
        "enabled",
        "fast-check",
        "disabled",
    )


def test_the_schedule_is_a_five_field_cron() -> None:
    schedule = load_yaml(LONGHORN_DEFAULTS)[
        "k3s_longhorn_snapshot_data_integrity_cronjob"
    ]
    assert len(schedule.split()) == 5, (
        f"Longhorn takes a five-field cron; got {schedule!r}"
    )
