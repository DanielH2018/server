"""`verify` during a Longhorn hop: when a claim's failure is provisional rather than final.

THE STORAGE ROLLOUT (#5997). Longhorn's admission webhook, which every PersistentVolumeClaim's
dry run calls, is served by the longhorn-manager pods, and the manager DaemonSet replaces both
at once. A Kustomization holding a claim that reconciles in that window fails its dry run and
posts `failure`, then recovers on its next retry. So while STORAGE_TAG is expected, a
`failure` or `error` on another name ends the wait only once `flux/longhorn` has read
`success` for `storage_grace_s`: two of the bundle's largest `retryInterval`, one for the
retry and one for it to queue and post. A `failure` of STORAGE_TAG itself stays final. The
cost is that a real failure during a Longhorn hop holds that much later.

`verify_promotion.expected_set` reads `grace_s` into `Expected.storage_grace_s`, and
`verify_promotion.verify` asks a `Window` whether the failures it read end the wait.

Run: imported only. `uv run pytest scripts/deploy_tools/tests/test_verify_promotion_storage_rollout.py`
"""

import sys as _sys
from collections.abc import Iterable, Mapping
from pathlib import Path as _Path

# `chained_timeout` imports bare, from scripts/deploy_tools.
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
from chained_timeout import largest_retry_s

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


class Window:
    """One `verify` wait's view of the storage rollout: whether STORAGE_TAG has settled.

    Attributes:
        grace_s: the seconds a failure stays provisional after STORAGE_TAG reads `success`,
            0 when STORAGE_TAG is not expected.
        settled_at: the clock reading of the first poll that read STORAGE_TAG `success`, or
            None before one.
    """

    def __init__(self, names: Iterable[str], grace_s: int) -> None:
        self.grace_s = grace_s if STORAGE_TAG in set(names) else 0
        self.settled_at: float | None = None

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
        return self.settled_at is None or now - self.settled_at < self.grace_s

    def note(self) -> str:
        """The clause `verify` appends to its status line while a failure is provisional."""
        return (
            f"; provisional while {STORAGE_TAG} rolls, until {self.grace_s}s after "
            f"{STATUS_PREFIX}{STORAGE_TAG} reads success"
        )
