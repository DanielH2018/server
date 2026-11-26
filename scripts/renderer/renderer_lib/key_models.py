"""Pydantic models that name the keys a renderer mapping may carry, and refuse every other key.

A model types each value `Any` and only validates: the render code that reads a key refuses a
wrong shape with a message naming what the key means, and the caller passes the YAML mapping on
itself, so no model default reaches a reader.
"""

from collections.abc import Iterable, Mapping
from typing import Any, NamedTuple

import pydantic


class KeyErrors(NamedTuple):
    missing: list[str]
    unknown: list[str]


def keys_model(
    name: str, keys: Iterable[str], required: Iterable[str] = ()
) -> type[pydantic.BaseModel]:
    """A model taking any value for each of `keys` and `required`, and refusing every other key."""
    fields: dict[str, Any] = {key: (Any, None) for key in keys}
    fields |= {key: (Any, ...) for key in required}
    return pydantic.create_model(
        name, __config__=pydantic.ConfigDict(extra="forbid"), **fields
    )


def key_errors(model: type[pydantic.BaseModel], mapping: Mapping) -> KeyErrors:
    """The required keys `mapping` lacks, and the keys it carries that `model` does not name."""
    try:
        model.model_validate(mapping)
    except pydantic.ValidationError as exc:
        errors = exc.errors()
        return KeyErrors(
            sorted(str(e["loc"][0]) for e in errors if e["type"] == "missing"),
            sorted(str(e["loc"][0]) for e in errors if e["type"] == "extra_forbidden"),
        )
    return KeyErrors([], [])
