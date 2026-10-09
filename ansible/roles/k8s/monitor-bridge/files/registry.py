"""The check registry: every check that runs, with its Kuma push token and its body.

`build_checks(env)` is the whole module. Which checks exist, in what order and with which body
is `check_table.CHECKS`; this turns each row without `is_gate` into a `Check` carrying the token
read from `env`. The four gate rows are `gates.py`'s, which evaluates them before any check.

It takes the environment as a PARAMETER rather than reading `os.environ` at import, which is
what lets `main(argv, env={...})` decide which monitor a result is pushed to.

This module is a LEAF: it imports `bridge.types` and `check_table`, and never `check`, `gates`
or `cli`. Each token is read from `bridge.types.push_env(name)`, the `KUMA_PUSH_<NAME>` the
env-secret renders for the row of the same name.
"""

import os
from collections.abc import Mapping

from bridge.types import Check, push_env
from check_table import CHECKS


def build_checks(env: Mapping[str, str] | None = None) -> list[Check]:
    """Every check, in evaluation order, with its push token read from `env`.

    Args:
      env: The environment the `KUMA_PUSH_*` tokens are read from. None reads `os.environ`,
        which is what the pod does.

    Returns:
      A fresh list — the caller owns it, so a test can hand `run_once` a different one without
      mutating anything shared.
    """
    e = os.environ if env is None else env
    return [
        Check(row.name, e.get(push_env(row.name), ""), row.fn)
        for row in CHECKS
        if not row.is_gate
    ]
