"""The verdict a landing reaches when its diff derivation refuses the range as broad.

A truncated file list sends the landing to `deploy.sh --changed`, which refuses a broad
range, and `land.sh` must still end with a `VERDICT:` line. Since D6.12 such a range waits on
the promotion, whose changed set names its k8s tags, and then grades from the deployer's
markers. The pairs here are the three ways that range can end: the tick's marker covers it,
the marker was read mid-apply, or the tick is not the apply at all.

Run: uv run pytest scripts/deploy_tools/tests/test_land_broad_fallback_verdict.py
"""

import pytest

from _land_fakes import MERGE_SHA, Fakes, receipt
from lib.exit_codes import DEPLOY_BROAD
from deploy_tools.land_lib import deploy
from deploy_tools.land_lib.outcome import Outcome
from deploy_tools.land_lib.landing import Classification


def _ready(landing, fakes=None, **opts):
    ln, calls = landing(fakes, **opts)
    ln.merge_sha = MERGE_SHA
    ln.ledger.t_ci = 2.0
    ln.ledger.t_tick = 3.0
    return ln, calls


def _broad_fallback(landing, fakes, **opts):
    """A landing on the truncated-file-list path whose `deploy_tags.py changed` refuses."""
    ln, calls = _ready(landing, fakes, since="beefbeef", **opts)
    ln.needs_diff = True
    ln.classification = Classification(
        self_applied=True,
    )
    return ln, calls


def test_a_broad_fallback_grades_from_the_deployers_markers(landing):
    """A landing must end with a `VERDICT:` line while the tick is applying its own merge
    commit; its receipt records that commit minutes later."""
    ln, _calls = _broad_fallback(
        landing,
        Fakes(
            changed_rc=DEPLOY_BROAD,
            state=receipt({"ansible/deploy.yml": []}),
            merge_applied_rc=0,
        ),
    )
    with pytest.raises(Outcome) as exc:
        deploy.deploy_phase(ln)
    assert (exc.value.verdict, exc.value.rc) == ("settled", 0)
    assert [c[1][:2] for c in _calls if c[0] == "land_wait"] == [(MERGE_SHA, [])]


def test_a_broad_fallback_over_an_abandoned_tick_watch_is_deferred(landing):
    """The receipt's `applied` is written only after the apply RETURNS, and a tick that already
    ff-merged this PR answers CONVERGED rather than BEHIND — so the receipt read mid-apply is
    not evidence the tick skipped it."""
    ln, _calls = _broad_fallback(
        landing, Fakes(changed_rc=DEPLOY_BROAD, state={}, merge_applied_rc=0)
    )
    ln.tick_watch_abandoned = True
    with pytest.raises(Outcome) as exc:
        deploy.deploy_phase(ln)
    assert (exc.value.verdict, exc.value.rc) == ("deferred", 75)
    assert "stopped watching a tick still applying" in exc.value.detail


def test_a_broad_fallback_the_tick_does_not_apply_still_names_a_verdict(landing):
    """`--since` bounds a range wider than this PR, so another session's broad merge can
    refuse the derivation for a PR that touched nothing broad. It waits on the promotion,
    and a changed set naming no tag ends it with a `VERDICT:` line."""
    ln, calls = _broad_fallback(landing, Fakes(changed_rc=DEPLOY_BROAD))
    ln.classification = Classification(
        self_applied=False,
    )
    with pytest.raises(Outcome) as exc:
        deploy.deploy_phase(ln)
    assert (exc.value.verdict, exc.value.rc) == ("nothing-to-deploy", 0)
    assert "land_wait" in [c[0] for c in calls]


def test_a_broad_fallback_derives_no_tag_and_runs_no_narrowing(landing):
    """The deployer's `narrow` scoped such a range to k8s tags until D6.12; Flux applies all
    of them, so the landing asks it nothing and the changed set names them."""
    ln, calls = _broad_fallback(
        landing,
        Fakes(changed_rc=DEPLOY_BROAD, narrowed_rc=0, narrowed="bazarr,sonarr\n"),
    )
    deploy.derive_from_diff(ln)
    assert ln.resolved_tags == []
    assert [c[1] for c in calls if c[0] == "deploy_tags"] == [("changed", "beefbeef")]
