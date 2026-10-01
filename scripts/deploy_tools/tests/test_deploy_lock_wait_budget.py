#!/usr/bin/env python3
"""The wrapper's LOCK_WAIT must outlast the deployer's worst-case hold of the tree lock.

`deploy.sh` queues behind `gitops-deploy.service` rather than giving up, so its wait has to
cover the longest that unit can legitimately hold the lock: the staging gate, the staging
expect, the k8s deploy and the k8s rollback, which run sequentially inside one activation.
A LOCK_WAIT below that sum makes a deploy launched during a pathological gitops run give up
having deployed nothing.

The four values are read from the role defaults the unit renders its
`config.env` from, not copied here, so raising any one of them fails this rather than
silently shortening the wait again. The value read is `deploy_under_locks.LOCK_WAIT`,
which both the foreground and `--detach` wait with.

Run: uv run pytest scripts/deploy_tools/tests/test_deploy_lock_wait_budget.py
"""

import pytest

from deploy_tools import deploy_under_locks
from lib import yaml_fast

from lib.repo_paths import REPO as _REPO

_DEFAULTS = (
    _REPO / "ansible" / "roles" / "setup" / "gitops_deploy" / "defaults" / "main.yml"
)

# The two phases of one gitops-deploy activation, in the order `config.env.j2` renders them.
# Named rather than globbed for a `_timeout_s` suffix: a new unrelated timeout must not
# silently join the sum, and a renamed one must fail here rather than drop out of it.
_HOLD_KEYS = (
    "gitops_deploy_k8s_timeout_s",
    "gitops_deploy_k8s_rollback_timeout_s",
)


def _worst_case_hold(defaults: dict) -> int:
    """The seconds one gitops-deploy activation can hold the tree lock for.

    Args:
        defaults: the parsed `gitops_deploy` role defaults.

    Returns:
        The sum of the sequential phase timeouts.

    Raises:
        KeyError: a phase timeout the deployer still reads is missing from `defaults`.
    """
    return sum(int(defaults[key]) for key in _HOLD_KEYS)


def _lock_wait() -> int:
    return deploy_under_locks.LOCK_WAIT


@pytest.fixture(scope="module")
def defaults() -> dict:
    return yaml_fast.safe_load(_DEFAULTS.read_text())


def test_deploy_sh_lock_wait_clears_the_deployers_worst_case_hold(defaults):
    """CLEAN half, plus the non-vacuity the sum needs to mean anything."""
    missing = [key for key in _HOLD_KEYS if key not in defaults]
    assert not missing, f"{missing} no longer in the role defaults -- renamed?"
    hold = _worst_case_hold(defaults)
    assert hold > 0
    assert _lock_wait() >= hold, (
        f"LOCK_WAIT={_lock_wait()} is under the deployer's worst-case hold of {hold}s, so "
        "a deploy queued behind a slow gitops run gives up having deployed nothing"
    )


def test_a_raised_phase_timeout_raises_the_derived_hold(defaults):
    """FLAGGED half: a derivation that stopped reading the file would not move here."""
    raised = dict(defaults)
    raised["gitops_deploy_k8s_rollback_timeout_s"] = (
        int(defaults["gitops_deploy_k8s_rollback_timeout_s"]) + 600
    )
    assert _worst_case_hold(raised) == _worst_case_hold(defaults) + 600


def test_a_missing_phase_timeout_is_an_error_not_a_smaller_sum(defaults):
    """FLAGGED half: `.get(key, 0)` would read green on a renamed variable."""
    without = {k: v for k, v in defaults.items() if k != "gitops_deploy_k8s_timeout_s"}
    with pytest.raises(KeyError):
        _worst_case_hold(without)
