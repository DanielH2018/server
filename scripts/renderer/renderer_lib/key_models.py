"""Pydantic models that name the keys a renderer mapping may carry, and refuse every other key.

A model types each value `Any` and only validates: the render code that reads a key refuses a
wrong shape with a message naming what the key means, which a type error raised here first would
replace. A caller keeps the YAML mapping itself and passes it on, so no model default reaches a
reader. `key_errors` names each missing and each unknown key, for the caller's own message.

Typical usage example:

    Entry = keys_model("Entry", ("name", "namespace"), required=("name",))
    missing, unknown = key_errors(Entry, entry)
"""

from collections.abc import Iterable, Mapping
from typing import Any, NamedTuple

import pydantic


class KeyErrors(NamedTuple):
    """The keys a mapping lacks and the keys it carries that its model does not name."""

    missing: list[str]
    unknown: list[str]


def keys_model(
    name: str, keys: Iterable[str], required: Iterable[str] = ()
) -> type[pydantic.BaseModel]:
    """A model taking any value for each of `keys` and `required`, and refusing every other key.

    Args:
        name: The model's class name.
        keys: The keys a mapping may omit.
        required: The keys a mapping must carry.
    """
    fields: dict[str, Any] = {key: (Any, None) for key in keys}
    fields |= {key: (Any, ...) for key in required}
    return pydantic.create_model(
        name, __config__=pydantic.ConfigDict(extra="forbid"), **fields
    )


def key_errors(model: type[pydantic.BaseModel], mapping: Mapping) -> KeyErrors:
    """The keys `mapping` lacks of `model`'s required ones, and those `model` does not name, sorted."""
    try:
        model.model_validate(mapping)
    except pydantic.ValidationError as exc:
        errors = exc.errors()
        return KeyErrors(
            sorted(str(e["loc"][0]) for e in errors if e["type"] == "missing"),
            sorted(str(e["loc"][0]) for e in errors if e["type"] == "extra_forbidden"),
        )
    return KeyErrors([], [])
