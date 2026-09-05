"""The two types the registry and the run loop pass between them.

`Check` is one registry entry, `CheckResult` is what evaluating one produces, and `CheckFn` is
the signature every check body and every gate probe shares. They live here rather than in
`check.py` so `registry.py` can name them without importing the run loop — the registry is a
leaf, and an import back into `check.py` would make the two mutually dependent.

Stdlib only, like every module under files/.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import NamedTuple

from bridge.config import Config

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
