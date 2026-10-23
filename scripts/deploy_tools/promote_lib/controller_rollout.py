"""`verify` during a Flux hop: when the controller's own replacement makes a failure provisional.

THE CONTROLLER ROLLOUT (#6013). A promotion whose changed set includes `flux` can change
kustomize-controller's own Deployment. The old pod is replaced mid-reconcile and posts
`failure` ("health check failed") for the `flux` Kustomization and for `soak`, which was
running on it. The new pod posts `success` for `flux` about 12 seconds later and re-runs the
soak. On 627fd395 (2026-10-23, promote run 39977616897) `verify` read the first failure,
ended and held `failed=flux,soak`.

So while STAGE_TAG is expected, a `failure` or `error` of STAGE_TAG or of a name in
ALSO_REPLACED is provisional for GRACE_S after the first poll that read it: `verify` keeps
waiting for a later status on the same name. A `success` inside the grace ends the wait as
good. With none, the failure turns final at the grace's end, and `verify` fails as it did
before. Flux posts no timestamp that `verify` reads, so the poll that first reads a failure
stands in for the time it was posted, which it trails by at most one poll.

DECIDED: the grace is a fixed 15 minutes, not `verify`'s timeout. That timeout runs to hours
(`chained_timeout.VERIFY_CAP_S`), and a real `flux` failure would hold that long. 15 minutes
is the controller's rollout plus one 10-minute reconcile interval for the soak to re-run.
The names are fixed too. Notification-controller's status description carries only the event
reason, so `verify` cannot tell the replaced pod's "health check failed" from a real one, and
a name outside this set that fails during the hop stays final.

Run: imported only. `uv run pytest scripts/deploy_tools/tests/test_verify_promotion_controller_rollout.py`
"""

from collections.abc import Iterable

# The Kustomization that deploys kustomize-controller, so its presence in the changed set is
# what makes the controller's own Deployment a candidate for change.
STAGE_TAG = "flux"
# The names whose reconcile the replaced pod was running, besides STAGE_TAG.
ALSO_REPLACED = frozenset({"soak"})


GRACE_S = 15 * 60


class Window:
    """One `verify` wait's view of the controller rollout: when each failure was first read.

    Attributes:
        first_read: the clock reading of the first poll that read each name's current failure.
    """

    def __init__(self) -> None:
        self.first_read: dict[str, float] = {}

    def provisional(
        self, expected: Iterable[str], failed: Iterable[str], now: float
    ) -> frozenset[str]:
        """The names in `failed` whose failure is provisional at `now`, because the controller may roll.

        Empty when STAGE_TAG is not among `expected`, which makes every failure final. A name
        that left `failed` forgets its reading, so a later failure starts a new grace.
        """
        failed = frozenset(failed)
        self.first_read = {n: t for n, t in self.first_read.items() if n in failed}
        if STAGE_TAG not in set(expected):
            return frozenset()
        candidates = failed & ({STAGE_TAG} | ALSO_REPLACED)
        for name in candidates:
            self.first_read.setdefault(name, now)
        return frozenset(n for n in candidates if now - self.first_read[n] < GRACE_S)

    def note(self) -> str:
        """The clause `verify` appends to its status line while only these failures stand."""
        return (
            f"; provisional while kustomize-controller rolls, for {GRACE_S}s after the "
            "first read"
        )
