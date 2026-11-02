"""The types the registry, the run loop and the HTTP fetchers pass between them.

`PushCheck` is one row of the check table, `Check` is one registry entry, `CheckResult` is what
evaluating one produces, and `CheckFn` is the signature every check body and every gate probe
shares. They live here rather than in
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
from typing import TYPE_CHECKING, NamedTuple, Protocol

from bridge.config import Config

if TYPE_CHECKING:
    # Annotation only: bridge.sources imports bridge.net, which imports this module.
    from bridge.sources import Sources

type JsonValue = (
    None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
)
type JsonObject = dict[str, JsonValue]

# Every check body and every gate probe: the frozen config, and the sources it reads.
CheckFn = Callable[[Config, "Sources"], tuple[bool, str]]


class VerdictRecorder(Protocol):
    """Where `check.run_once` records each enabled row's verdict: `CheckExposition` in the pod."""

    def record(self, name: str, ok: bool, now: float) -> None: ...


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
      fn: The check body. Takes the frozen `Config` and the `Sources`, and returns (ok, msg).
      gate: The row's `gate` column. A reachability gate's name (`b2_reachable`, `wan_reachable`)
        skips this row while that gate row reads down, and any other value skips nothing.
        `check.run_once` reads it, so the gate sets are never stated twice.
    """

    name: str
    fn: CheckFn
    gate: str | None = None


@dataclass(frozen=True)
class PushCheck:
    """One row of `check_table.CHECKS`: a check or gate.

    Every field but `fn` must be written as a LITERAL in the table. The deploy reads the table
    with `ansible/filter_plugins/py_table.py`, which parses `check_table.py` rather than running
    it, and keeps only the keyword arguments whose value is a literal. `fn` is the one it drops.

    Attributes:
      name: The check's own name — what CHECKS_ONLY/CHECKS_SKIP and the gate sets refer to.
      fn: The check body, or the gate's probe for a row with `is_gate`.
      gate: The gate whose outage suppresses this check (`b2_reachable`, `wan_reachable`),
        `prometheus` for a row that reads Prometheus and sits under `EXPORTER_DEPENDENT`, or
        `startup_grace` for a reach-out check whose rule carries `for: 10m`. One value per row.
      is_gate: This row is one of the two reachability gates, which `check.run_once`
        evaluates first.
      critical: The row's rule carries `severity: critical` and mails as well as posts.
      runbook: The docs page slug the rule's `runbook_url` links to.
    """

    name: str
    fn: CheckFn
    gate: str | None = None
    is_gate: bool = False
    critical: bool = False
    runbook: str | None = None


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
