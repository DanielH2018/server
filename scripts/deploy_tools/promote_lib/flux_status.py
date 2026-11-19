"""The `flux/<name>` commit statuses Flux's notification controller posts, read by name.

`forge.Forge.statuses` returns every context on a commit as raw strings. `flux_states` keeps
the `flux/` contexts, strips the prefix and parses each state, so `verify`, `decide`'s record
of `prod`'s statuses and its supersede check look a Kustomization up by its own name and
compare against StatusState.

Run: imported only. `uv run pytest scripts/deploy_tools/tests/test_verify_promotion.py`
"""

from collections.abc import Mapping
from enum import StrEnum

STATUS_PREFIX = "flux/"


class StatusState(StrEnum):
    """A commit status's state, as GitHub names it."""

    PENDING = "pending"
    SUCCESS = "success"
    FAILURE = "failure"
    ERROR = "error"


FAILED_STATES = frozenset({StatusState.FAILURE, StatusState.ERROR})


def flux_states(statuses: Mapping[str, str]) -> dict[str, StatusState]:
    """Each `flux/<name>` context in `statuses`, keyed by `<name>`.

    A context outside `flux/` is left out, and so is a state GitHub does not define: the
    caller reads an absent name as no status, never as success.
    """
    states = {}
    for context, state in statuses.items():
        name = context.removeprefix(STATUS_PREFIX)
        if name != context and state in StatusState:
            states[name] = StatusState(state)
    return states
