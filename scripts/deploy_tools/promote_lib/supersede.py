"""`decide` promoting T over a verification in flight: when it may, and against what (D7.5).

While `prod` and `last-good` name different digests, a run's verification is in flight, and
`decide` waits on it (`verify_promotion.wait_settled`). With `promote.SUPERSEDE` on, it may
instead promote T over P, `prod`'s commit, when every condition below holds. T's bundle
carries P's changes, so T's verification takes its changed set against `last-good` and covers
both, and P's run prints `superseded` and writes nothing (`superseded.py`, D7.1).

THE CONDITIONS. `verify_promotion.wait_settled` reads the first two and asks
`supersedable` the third; `refusal` reads the rest once T is chosen:
  - DEPLOY_HOLD reads `none`;
  - `prod` is in flight: it differs from `last-good` and is not the `held` digest;
  - `prod` is not the `superseding` digest, so P did not itself supersede a run and a
    verification is superseded at most once (`artifact_retention.SUPERSEDING_TAG`);
  - no name in P's set, P against `last-good`, reads `failure` or `error` on P's commit;
  - T's own change, T against P, shares no name with P's set but `homelab` and `soak`;
  - neither holds `longhorn`, `flux` or `cluster-vars`, each of which has its own window in
    `verify`, and neither was widened to every name;
  - T names no forward-only tag and no snapshot role against `last-good`, checked by
    `decide` on the promotion `_promotion` returns.
A condition that fails returns `decide` to the wait, which promotes T once P settles.
docs/target-architecture/deploy/d7-superseding-promotions.md has the design and the cases.

Run: imported only. `uv run pytest scripts/deploy_tools/tests/test_promote_supersede.py`
"""

import sys as _sys
from collections.abc import Callable
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
# `changed_set` and `artifact_retention` import bare, from scripts/deploy_tools.
_sys.path.insert(1, str(_Path(__file__).resolve().parents[1]))
from artifact_retention import SUPERSEDING_TAG
from changed_set import ROOT, SOAK, ChangedSet, changed_set
from deploy_tools.promote_lib import verify_promotion as verify
from deploy_tools.promote_lib.reads import Reads

# Each has its own provisional window in `verify`, which a superseding promotion does not
# compose: the storage rollout, the controller rollout, and the cluster file's substitution.
OWN_WINDOW = frozenset({"longhorn", "flux", "cluster-vars"})
# Every changed set beside a tag holds these, so they never make two sets overlap.
ALWAYS = frozenset({ROOT, SOAK})
FAILED_STATES = frozenset({"failure", "error"})


def supersedable(reads: Reads) -> Callable[[str], bool]:
    """Whether T may take over the run `prod`'s digest names: it is not the `superseding` one.

    `verify_promotion.wait_settled` asks it only once it has read DEPLOY_HOLD as `none` and
    `prod` in flight and not `held`, so those reads are made once a poll.
    """
    return lambda prod: reads.digest(SUPERSEDING_TAG) != prod


def _changed(new: dict[str, bytes], old: dict[str, bytes]) -> ChangedSet:
    """`new`'s changed set against `old`, with `new`'s `dependsOn` and suspended names."""
    depends_on, suspended = verify.bundle_graph(new, verify.flux_kustomizations(new))
    return changed_set(
        verify.bundle_digests(new), verify.bundle_digests(old), depends_on, suspended
    )


def refusal(
    reads: Reads,
    prod: str | None,
    prod_bundle: dict[str, bytes] | None,
    last_good_bundle: dict[str, bytes] | None,
    target_bundle: dict[str, bytes] | None,
) -> str:
    """Why T may not supersede P, or "" when it may.

    Args:
        reads: the reads `decide` makes; `statuses` reads P's commit.
        prod: P, `prod`'s commit.
        prod_bundle: P's bundle.
        last_good_bundle: `last-good`'s bundle.
        target_bundle: T's bundle.

    Raises:
        Whatever `reads.statuses` raises, so a failed read is `decide`'s `error`.
    """
    if (
        prod is None
        or prod_bundle is None
        or last_good_bundle is None
        or target_bundle is None
    ):
        return "decide read no commit or bundle for P, last-good or T"
    p_set = _changed(prod_bundle, last_good_bundle)
    t_own = _changed(target_bundle, prod_bundle)
    for name, found in (("P's set", p_set), ("T's own change", t_own)):
        if found.widened:
            return f"{name} widened to every name: {found.widened}"
        if own := sorted(found.names & OWN_WINDOW):
            return f"{name} holds {', '.join(own)}, which verify waits on in its own window"
    if overlap := sorted((t_own.direct & p_set.names) - ALWAYS):
        return f"T changes {', '.join(overlap)}, which P is still verifying"
    states = reads.statuses(prod)
    if failing := sorted(
        name
        for name in p_set.names | {ROOT}
        if states.get(verify.STATUS_PREFIX + name) in FAILED_STATES
    ):
        return f"P already reads failure for {', '.join(failing)}, so P settles first"
    return ""
