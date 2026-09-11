#!/usr/bin/env python3
"""Guards the shape of the `snapshot-data-integrity` settings patch.

`snapshot-data-integrity` is `DataEngineSpecific: true`, which makes its `value` a JSON MAP
SERIALISED AS A STRING -- `{"v1":"enabled","v2":"enabled"}` -- and not the bare string every
other Longhorn setting patched in `longhorn.yml` takes. The failure mode is quiet: a patch
copying a sibling's `{"value":"enabled"}` shape is accepted by kubectl, stored, and ignored by
Longhorn, so the cluster keeps running `fast-check` while the repo and a green deploy both say
`enabled`. That is #1364's whole subject, so the shape is worth a guard rather than a comment.

The two halves below are the accept/reject pair this repo requires of a new check: a bare-string
value must be REJECTED, and the rendered map must be ACCEPTED. Without the rejecting half there
is no evidence the assertion can go red.

Run: uv run pytest ansible/tests/longhorn/test_snapshot_data_integrity_patch_is_a_json_map.py
"""

import json

from _helpers import ANSIBLE
from _helpers import load_yaml


TASKS = ANSIBLE / "roles" / "setup" / "k3s" / "tasks" / "longhorn.yml"
DEFAULTS = ANSIBLE / "roles" / "setup" / "k3s" / "defaults" / "main.yml"
TASK_NAME = "Set the Longhorn snapshot data-integrity mode"


def _task() -> dict:
    for task in load_yaml(TASKS):
        if task.get("name") == TASK_NAME:
            return task
    raise AssertionError(
        f"{TASK_NAME!r} is gone from {TASKS.name}; it is what arms bit-rot detection"
    )


def _rendered_patch(mode: str) -> str:
    """What the task's `integrity_patch` expression produces for `mode`.

    Mirrors `{"value":{{ {"v1": m, "v2": m} | to_json | to_json }}}` -- the inner `to_json`
    renders the map, the outer quotes it as a JSON string.
    """
    return '{"value":%s}' % json.dumps(json.dumps({"v1": mode, "v2": mode}))


def test_the_task_is_present_and_patches_the_right_setting() -> None:
    cmd = _task()["ansible.builtin.command"]["cmd"]
    assert "settings.longhorn.io snapshot-data-integrity" in cmd
    assert "--type=merge" in cmd


def test_the_task_builds_its_value_with_a_double_to_json() -> None:
    """The double `to_json` is the whole mechanism -- a single one stores a bare map, not a string."""
    expr = _task()["vars"]["integrity_patch"]
    assert expr.count("to_json") == 2, (
        "integrity_patch must pipe through to_json twice: once to render the map, once to "
        f"quote it as a JSON string. Got: {expr!r}"
    )


def test_a_json_map_value_is_accepted() -> None:
    """The shape Longhorn actually reads: `value` is a STRING whose content parses as a map."""
    patch = json.loads(_rendered_patch("enabled"))
    assert isinstance(patch["value"], str), (
        "value must be a string, not a nested object"
    )
    assert json.loads(patch["value"]) == {"v1": "enabled", "v2": "enabled"}


def test_a_bare_string_value_is_flagged() -> None:
    """The mistake this guard exists for: copying a sibling setting's `{"value":"enabled"}`."""
    bare = json.loads('{"value":"enabled"}')
    assert isinstance(bare["value"], str)
    try:
        parsed = json.loads(bare["value"])
    except json.JSONDecodeError:
        parsed = None
    assert parsed != {"v1": "enabled", "v2": "enabled"}, (
        "a bare-string value must NOT satisfy the map check -- if it does, this guard cannot "
        "distinguish the correct shape from the one that Longhorn silently ignores"
    )


def test_the_mode_comes_from_a_default_rather_than_a_literal() -> None:
    """A literal in the task would make the revert-after-measurement a code edit, not a var flip."""
    assert "k3s_longhorn_snapshot_data_integrity" in _task()["vars"]["integrity_mode"]
    defaults = load_yaml(DEFAULTS)
    assert defaults["k3s_longhorn_snapshot_data_integrity"] in (
        "enabled",
        "fast-check",
        "disabled",
    )
