"""`verify` during a Flux hop: when the controller's own replacement makes a failure provisional.

THE CONTROLLER ROLLOUT (#6013). A promotion whose changed set includes `flux` can change
kustomize-controller's own Deployment. The old pod is replaced mid-reconcile and posts
`failure` ("health check failed") for the `flux` Kustomization and for `soak`, which was
running on it. The new pod posts `success` for `flux` about 12 seconds later and re-runs the
soak. On 627fd395 (2026-10-23, promote run 39977616897) `verify` read the first failure,
ended and held `failed=flux,soak`.

So while STAGE_TAG is expected, a `failure` or `error` of STAGE_TAG or of a name in
ALSO_REPLACED is provisional: `verify` keeps waiting for a later status on the same name.
No separate bound applies. A failure that no `success` follows ends at `verify`'s own
timeout, naming every name that is not `success`. The cost is that a real `flux` failure
holds at that timeout rather than at the first read.

DECIDED: the names are fixed, not derived from the bundle. Notification-controller's status
description carries only the event reason, so `verify` cannot tell the replaced pod's
"health check failed" from a real one, and a name outside this set that fails during the hop
stays final.

Run: imported only. `uv run pytest scripts/deploy_tools/tests/test_verify_promotion_controller_rollout.py`
"""

from collections.abc import Iterable

# The Kustomization that deploys kustomize-controller, so its presence in the changed set is
# what makes the controller's own Deployment a candidate for change.
STAGE_TAG = "flux"
# The names whose reconcile the replaced pod was running, besides STAGE_TAG.
ALSO_REPLACED = frozenset({"soak"})


def provisional(expected: Iterable[str], failed: Iterable[str]) -> frozenset[str]:
    """The names in `failed` whose failure is provisional, because the controller may roll.

    Empty when STAGE_TAG is not among `expected`, which makes every failure final.
    """
    if STAGE_TAG not in set(expected):
        return frozenset()
    return frozenset(failed) & ({STAGE_TAG} | ALSO_REPLACED)


def note() -> str:
    """The clause `verify` appends to its status line while only these failures stand."""
    return (
        "; provisional while kustomize-controller rolls, until a later success "
        "or the timeout"
    )
