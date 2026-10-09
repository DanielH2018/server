"""The type of a parsed JSON document, and the narrowing helpers that go with it.

WHY. ``json.loads`` returns ``Any``, so every caller of ``kubectl_json`` and ``gh_json`` read
an untyped value and the type checker verified nothing about it. A recursive alias makes the
boundary honest: a caller must narrow with ``isinstance`` (or one of the helpers below)
before it indexes, so a response of the wrong shape fails where it is read.

``monitor-bridge`` is stdlib-only and ships its own files into a pod, so it cannot import this
module. ``bridge/types.py`` holds its own copy of the alias.
"""

type JsonValue = (
    None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
)
type JsonObject = dict[str, JsonValue]


def as_object(value: JsonValue, what: str) -> JsonObject:
    """``value`` as a JSON object, or ``ValueError`` naming ``what`` and the type received."""
    if not isinstance(value, dict):
        raise ValueError(f"{what}: expected a JSON object, got {type(value).__name__}")
    return value


def as_list(value: JsonValue, what: str) -> list[JsonValue]:
    """``value`` as a JSON array, or ``ValueError`` naming ``what`` and the type received."""
    if not isinstance(value, list):
        raise ValueError(f"{what}: expected a JSON array, got {type(value).__name__}")
    return value


def as_object_list(value: JsonValue, what: str) -> list[JsonObject]:
    """``value`` as a JSON array of objects, or ``ValueError`` naming ``what``.

    ``None`` reads as an empty list: ``gh_json`` returns it for empty output, which is how
    ``gh`` reports "no rows" to the callers that list.
    """
    if value is None:
        return []
    return [
        as_object(entry, f"{what}[{i}]") for i, entry in enumerate(as_list(value, what))
    ]
