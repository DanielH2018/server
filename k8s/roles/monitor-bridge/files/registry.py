"""The check registry: every check that runs, with its body and its gate.

`build_checks()` is the whole module. Which checks exist, in what order and with which body
is `check_table.CHECKS`; this turns each row into a `Check` carrying the row's `gate`. The two
gate rows come first, so `check.run_once` has each gate's verdict before it reaches a row that
names the gate.

This module is a LEAF: it imports `bridge.types` and `check_table`, and never `check` or
`cli`.
"""

from bridge.types import Check
from check_table import CHECKS


def build_checks() -> list[Check]:
    """Every check, gate rows first, then the rest in table order.

    Returns:
      A fresh list — the caller owns it, so a test can hand `run_once` a different one without
      mutating anything shared.
    """
    # `sorted` is stable, so the gate rows keep their table order among themselves.
    return [
        Check(row.name, row.fn, row.gate)
        for row in sorted(CHECKS, key=lambda r: not r.is_gate)
    ]
