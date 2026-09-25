"""The verdict a landing reaches when its diff derivation refuses the range as broad.

PR #2437 (330 files) ended with no `VERDICT:` line: the truncated file list sent it to
`deploy.sh --changed`, which refused the broad range, and `land.sh` exited 1 telling the
session to deploy by hand — while the tick it had kicked was applying that very merge commit
(#2448). The pairs here are the three ways that range can end: the tick's marker covers it,
the marker was read mid-apply, or the tick is not the apply at all.

Run: uv run pytest scripts/deploy_tools/tests/test_land_broad_fallback_verdict.py
"""

import pytest

from _land_fakes import MERGE_SHA, Fakes
from deploy_tools.exit_codes import DEPLOY_BROAD
from deploy_tools.land_lib import deploy
from deploy_tools.land_lib.outcome import Outcome


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
    ln.self_applied = True
    return ln, calls


def test_a_broad_fallback_grades_from_the_deployers_markers(landing):
    """PR #2437 exited 1 with no `VERDICT:` line while the tick was applying its own merge
    commit; `broad_applied` read that commit minutes later (#2448)."""
    ln, _calls = _broad_fallback(
        landing,
        Fakes(
            changed_rc=DEPLOY_BROAD,
            state={"broad_applied": f"{MERGE_SHA} ansible/deploy.yml"},
            merge_applied_rc=0,
        ),
    )
    with pytest.raises(Outcome) as exc:
        deploy.deploy_phase(ln)
    assert (exc.value.verdict, exc.value.rc) == ("settled", 0)
    assert "no tag list scoping its diff" in exc.value.detail


def test_a_broad_fallback_over_an_abandoned_tick_watch_is_deferred(landing):
    """`broad_applied` is written only after the apply RETURNS, and a tick that already
    ff-merged this PR answers CONVERGED rather than BEHIND — so the marker read mid-apply is
    not evidence the tick skipped it (#2448)."""
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
    refuse the derivation for a PR that touched nothing broad. That one is still owed to a
    hand — but with a `VERDICT:` line, which is what #2448 is about."""
    ln, _calls = _broad_fallback(landing, Fakes(changed_rc=DEPLOY_BROAD))
    ln.self_applied = False
    with pytest.raises(Outcome) as exc:
        deploy.deploy_phase(ln)
    assert (exc.value.verdict, exc.value.rc) == ("needs-manual-apply", 1)


# ── the deployer's own narrowing, tried before the range is called broad (issue #2520) ──


def test_a_broad_fallback_the_narrowing_can_scope_deploys_those_tags(landing):
    """CLEAN half. `changed` refuses a range wholesale on ONE broad path, so a >100-file PR
    touching group_vars alongside a service role deployed nothing at all."""
    ln, calls = _broad_fallback(
        landing,
        Fakes(changed_rc=DEPLOY_BROAD, narrowed_rc=0, narrowed="bazarr,sonarr\n"),
    )
    deploy.derive_from_diff(ln)
    assert ln.resolved_tags == ["bazarr", "sonarr"]
    assert [c[1] for c in calls if c[0] == "deploy_tags"] == [
        ("changed", "beefbeef"),
        ("narrow", "beefbeef", "HEAD"),
    ]


def test_a_narrowing_that_reaches_nothing_deploys_nothing_rather_than_settling(landing):
    """Exit 0 with empty stdout is `narrow` answering "this range moves no rendered output".

    `deploy_phase` sends an empty tag list to `no_tag_outcome`, which grades from the
    deployer's markers — not to a deploy of nothing reported as settled.
    """
    ln, _calls = _broad_fallback(
        landing, Fakes(changed_rc=DEPLOY_BROAD, narrowed_rc=0, narrowed="\n")
    )
    deploy.derive_from_diff(ln)
    assert ln.resolved_tags == []
