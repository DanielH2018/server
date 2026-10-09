"""The types the registry, the run loop and the HTTP fetchers pass between them.

`Check` is one registry entry, `CheckResult` is what evaluating one produces, and `CheckFn` is
the signature every check body and every gate probe shares. They live here rather than in
`check.py` so `registry.py` can name them without importing the run loop — the registry is a
leaf, and an import back into `check.py` would make the two mutually dependent.

`JsonValue` is the type of a parsed HTTP response. `bridge.net._get_json` returns it rather than
`Any`, so a check must narrow (`as_object`, `as_list`, or its own `isinstance`) before it
indexes, and a source that answers with the wrong shape fails where it is read. The alias is a
copy of `scripts/lib/json_types.py`: this role ships only its own files/ into the pod.

Stdlib only, like every module under files/.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import NamedTuple

from bridge.config import Config

type JsonValue = (
    None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
)
type JsonObject = dict[str, JsonValue]

CheckFn = Callable[[Config], tuple[bool, str]]  # every check body and every gate


class CheckResult(NamedTuple):
    """What a check or a gate decided: `ok` and the message pushed to Kuma.

    A NamedTuple rather than a dataclass so every existing `ok, msg = fn()` unpacking — in
    the run loop, in the checks modules and across the test suite — keeps working unchanged.
    The check bodies themselves still return a plain `(bool, str)` tuple, which this type
    accepts; the name exists so the registry's consumption boundary says what the pair means.
    """

    ok: bool
    msg: str


@dataclass(frozen=True)
class Check:
    """One entry in the check registry `registry.build_checks` returns.

    Attributes:
      name: The check's own name — what CHECKS_ONLY/CHECKS_SKIP and the gate sets refer to.
      token: The Kuma push-monitor token this check's result is pushed to. Empty skips the push.
      fn: The check body. Takes the frozen `Config` and returns (ok, msg).
    """

    name: str
    token: str
    fn: CheckFn


def as_object(value: JsonValue, what: str) -> JsonObject:
    """`value` as a JSON object, or `RuntimeError` naming `what` and the type received."""
    if not isinstance(value, dict):
        raise RuntimeError(
            "%s: expected a JSON object, got %s" % (what, type(value).__name__)
        )
    return value


def as_list(value: JsonValue, what: str) -> list[JsonValue]:
    """`value` as a JSON array, or `RuntimeError` naming `what` and the type received."""
    if not isinstance(value, list):
        raise RuntimeError(
            "%s: expected a JSON array, got %s" % (what, type(value).__name__)
        )
    return value


def as_object_list(value: JsonValue, what: str) -> list[JsonObject]:
    """`value` as a JSON array whose every entry is an object, or `RuntimeError` naming `what`."""
    return [
        as_object(entry, "%s[%d]" % (what, i))
        for i, entry in enumerate(as_list(value, what))
    ]


def optional_object(value: JsonValue, what: str) -> JsonObject | None:
    """`value` as a JSON object, `None` when it is JSON null, or `RuntimeError` otherwise.

    For the verdict functions that take `dict | None` and report a missing payload themselves.
    """
    return None if value is None else as_object(value, what)
