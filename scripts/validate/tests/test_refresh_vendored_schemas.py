"""The pure halves of the schema refresher: the tag it derives, and what it keeps of a file.

Run: uv run pytest scripts/validate/tests/test_refresh_vendored_schemas.py
"""

import json

import pytest

from validate.refresh_vendored_schemas import (
    kubernetes_tag,
    vendor_core_file,
    without_descriptions,
)


def test_the_kubernetes_tag_is_the_k3s_version_without_its_suffix():
    assert kubernetes_tag("x: 1\nk3s_version: v1.36.4+k3s1\n") == "v1.36.4"


def test_a_defaults_file_with_no_k3s_version_is_refused():
    with pytest.raises(ValueError, match="k3s_version not found"):
        kubernetes_tag("k3s_channel: stable\n")


def test_descriptions_are_dropped_but_a_field_named_description_stays():
    schema = {
        "description": "an object",
        "properties": {
            "description": {"type": "string", "description": "the field's own text"}
        },
    }
    assert without_descriptions(schema) == {
        "properties": {"description": {"type": "string"}}
    }


def _upstream(schemas: dict) -> bytes:
    return json.dumps(
        {"paths": {"/x": {}}, "components": {"schemas": schemas}}
    ).encode()


def test_a_self_contained_file_is_vendored_as_schemas_only():
    body = _upstream(
        {
            "A": {
                "description": "d",
                "properties": {"b": {"$ref": "#/components/schemas/B"}},
            },
            "B": {"type": "string"},
        }
    )
    vendored = json.loads(vendor_core_file(body))
    assert vendored == {
        "components": {
            "schemas": {
                "A": {"properties": {"b": {"$ref": "#/components/schemas/B"}}},
                "B": {"type": "string"},
            }
        }
    }


def test_a_ref_outside_the_file_is_refused():
    body = _upstream(
        {"A": {"properties": {"b": {"$ref": "#/components/schemas/Missing"}}}}
    )
    with pytest.raises(ValueError, match="outside the file"):
        vendor_core_file(body)
