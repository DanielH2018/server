"""The check registry: every check that runs, with its Kuma push token and its body.

`build_checks(env)` is the whole module. Which checks exist, in what order and with which body
is `check_table.CHECKS`; this turns each row into a `Check` carrying the token read from `env`
and the row's `gate`. The three gate rows come first, so `check.run_once` has each gate's
verdict before it reaches a row that names the gate.

It takes the environment as a PARAMETER rather than reading `os.environ` at import, which is
what lets `main(argv, env={...})` decide which monitor a result is pushed to.

This module is a LEAF: it imports `bridge.types` and `check_table`, and never `check` or
`cli`. Each token is read from `bridge.types.push_env(name)`, the `KUMA_PUSH_<NAME>` the
env-secret renders for the row of the same name.
"""

import os
from collections.abc import Mapping

from bridge.types import Check, push_env
from check_table import CHECKS


def build_checks(env: Mapping[str, str] | None = None) -> list[Check]:
    """Every check, gate rows first, then the rest in table order, with push tokens from `env`.

    Args:
      env: The environment the `KUMA_PUSH_*` tokens are read from. None reads `os.environ`,
        which is what the pod does.

    Returns:
      A fresh list — the caller owns it, so a test can hand `run_once` a different one without
      mutating anything shared.
    """
    e = os.environ if env is None else env
    # `sorted` is stable, so the gate rows keep their table order among themselves.
    return [
        Check(row.name, e.get(push_env(row.name), ""), row.fn, row.gate)
        for row in sorted(CHECKS, key=lambda r: not r.is_gate)
    ]
