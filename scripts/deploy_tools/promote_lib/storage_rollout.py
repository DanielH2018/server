"""`verify` during a Longhorn hop: when another name's failure is provisional, not final.

THE STORAGE ROLLOUT (#5997). Longhorn's admission webhook, which every PersistentVolumeClaim's
dry run calls, is served by the longhorn-manager pods. Since #6009 the manager DaemonSet rolls
one pod at a time, so the routine window in which no pod serves the webhook is gone. A residual
window remains: a call can still fail while the one serving pod is busy, or on a node loss. A
Kustomization holding a claim that reconciles in that window fails its dry run and posts
`failure`, then recovers on its next retry.

So while STORAGE_TAG is expected, a `failure` or `error` on ANY other expected name is
provisional, not only on a name that holds a claim. DECIDED: every name, because the failure
spreads past the claim holders: `crowdsec`'s failed claim holds `traefik` not-Ready, and
`traefik` every routed tag. Telling a claim holder from the rest would need each name's
`dependsOn` closure over T's claims, for a gain of at most the window below.

A provisional failure ends the wait in one of two ways:
  - once `flux/longhorn` has read `success` for `storage_grace_s`: two of the bundle's largest
    `retryInterval`, one for the retry and one for it to queue and post;
  - while `flux/longhorn` has not read `success`, once `storage_rollout_s` has passed since
    the first poll that read the failure: `longhorn`'s own `timeout` plus `storage_grace_s`.
    So a `longhorn` that hangs, or never posts, holds then rather than at `verify`'s timeout.
A `failure` of STORAGE_TAG itself stays final. The cost is that a real failure during a
Longhorn hop holds up to `storage_rollout_s` later, 14 minutes with a `10m` timeout and a
`2m` `retryInterval`.

DECIDED: keep the full `storage_rollout_s` hold after #6009 (operator, 2026-10-29, #6439). A
hop with no failure pays nothing. A real failure is already live when `verify` reads it, so
the hold only delays `settle`'s reset, while a transient read as final writes a hold that
only a hand clear lifts. More hops without provisional failures would not change that.

`verify_promotion.expected_set` reads `grace_s` and `rollout_s` into `Expected`, and
`verify_promotion.verify` asks a `Window` whether the failures it read end the wait.

Run: imported only. `uv run pytest scripts/deploy_tools/tests/test_verify_promotion_storage_rollout.py`
"""

import sys as _sys
from collections.abc import Iterable, Mapping
from pathlib import Path as _Path

# `chained_timeout` imports bare, from scripts/deploy_tools.
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
from chained_timeout import largest_retry_s, seconds

STATUS_PREFIX = "flux/"
# The tag whose longhorn-manager pods serve the admission webhook a claim's dry run calls
# (THE STORAGE ROLLOUT).
STORAGE_TAG = "longhorn"
# How many of the bundle's largest `retryInterval` a provisional failure has to recover after
# STORAGE_TAG reads `success`: one for the retry, one for it to queue behind the workers and
# post. On 61c16826 (2026-10-23) the four claims recovered 97s after `flux/longhorn` read
# `success`, on a 2-minute `retryInterval`.
STORAGE_GRACE_RETRIES = 2


def grace_s(docs: list[dict], names: Iterable[str]) -> int:
    """How long after STORAGE_TAG reads `success` another name's failure stays provisional.

    Args:
        docs: every Kustomization in T's bundle.
        names: the names `verify` expects.

    Returns:
        STORAGE_GRACE_RETRIES of the bundle's largest `retryInterval`, or 0 when STORAGE_TAG
        is not among `names`, which makes every failure final.

    Raises:
        ValueError: a `retryInterval` `chained_timeout` cannot read.
    """
    if STORAGE_TAG not in set(names):
        return 0
    return STORAGE_GRACE_RETRIES * largest_retry_s(docs)


def rollout_s(docs: list[dict], grace: int) -> int:
    """How long a failure stays provisional while STORAGE_TAG has not read `success`.

    Args:
        docs: every Kustomization in T's bundle.
        grace: `grace_s` for the same bundle; 0 returns 0.

    Returns:
        STORAGE_TAG's `timeout`, or its `interval` without one, as Flux reads it, plus
        `grace`. 0 when `grace` is 0 or the bundle holds no STORAGE_TAG Kustomization.

    Raises:
        ValueError: a duration `chained_timeout` cannot read.
    """
    for doc in docs:
        if grace and (doc.get("metadata") or {}).get("name") == STORAGE_TAG:
            spec = doc.get("spec")
            spec = spec if isinstance(spec, dict) else {}
            timeout = spec.get("timeout", spec.get("interval"))
            return grace + (0 if timeout is None else seconds(timeout))
    return 0


class Window:
    """One `verify` wait's view of the storage rollout: whether STORAGE_TAG has settled.

    Attributes:
        grace_s: the seconds a failure stays provisional after STORAGE_TAG reads `success`,
            0 when STORAGE_TAG is not expected.
        rollout_s: the seconds a failure stays provisional, from the first poll that read
            one, while STORAGE_TAG has not read `success`.
        settled_at: the clock reading of the first poll that read STORAGE_TAG `success`, or
            None before one.
        failed_at: the clock reading of the first poll that read a provisional failure, or
            None before one.
    """

    def __init__(self, names: Iterable[str], grace_s: int, rollout_s: int) -> None:
        self.grace_s = grace_s if STORAGE_TAG in set(names) else 0
        self.rollout_s = rollout_s
        self.settled_at: float | None = None
        self.failed_at: float | None = None

    def provisional(
        self, failed: Iterable[str], states: Mapping[str, str], now: float
    ) -> bool:
        """Whether the `failed` names read in `states` at `now` leave the wait going.

        A failure of STORAGE_TAG itself is never provisional.
        """
        if not self.grace_s or STORAGE_TAG in set(failed):
            return False
        if (
            self.settled_at is None
            and states.get(STATUS_PREFIX + STORAGE_TAG) == "success"
        ):
            self.settled_at = now
        if self.failed_at is None:
            self.failed_at = now
        if self.settled_at is None:
            return now - self.failed_at < self.rollout_s
        return now - self.settled_at < self.grace_s

    def note(self) -> str:
        """The clause `verify` appends to its status line while a failure is provisional."""
        return (
            f"; provisional while {STORAGE_TAG} rolls, until {self.grace_s}s after "
            f"{STATUS_PREFIX}{STORAGE_TAG} reads success, or {self.rollout_s}s after the "
            "first failure if it never does"
        )
