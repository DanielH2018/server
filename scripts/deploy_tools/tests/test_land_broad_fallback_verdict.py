"""The verdict a landing reaches when its diff derivation refuses the range as broad.

Run: uv run pytest scripts/deploy_tools/tests/test_land_broad_fallback_verdict.py
"""

import pytest

from _land_fakes import MERGE_SHA, Fakes
from lib.exit_codes import DEPLOY_BROAD
from deploy_tools.land_lib import deploy
from deploy_tools.land_lib.outcome import Outcome
from deploy_tools.land_lib.landing import Classification


def _ready(landing, fakes=None, **opts):
    ln, calls = landing(fakes, **opts)
    ln.merge_sha = MERGE_SHA
    ln.ledger.t_ci = 2.0
    return ln, calls


def _broad_fallback(landing, fakes, **opts):
    """A landing on the truncated-file-list path whose `deploy_tags.py changed` refuses."""
    ln, calls = _ready(landing, fakes, since="beefbeef", **opts)
    ln.needs_diff = True
    ln.classification = Classification()
    return ln, calls


def test_a_broad_fallback_still_names_a_verdict(landing):
    """`--since` bounds a range wider than this PR, so another session's broad merge can
    refuse the derivation for a PR that touched nothing broad. It waits on the promotion,
    and a changed set naming no tag ends it with a `VERDICT:` line."""
    ln, calls = _broad_fallback(landing, Fakes(changed_rc=DEPLOY_BROAD))
    with pytest.raises(Outcome) as exc:
        deploy.deploy_phase(ln)
    assert (exc.value.verdict, exc.value.rc) == ("nothing-to-deploy", 0)
    assert "land_wait" in [c[0] for c in calls]


def test_a_broad_fallback_derives_no_tag_and_asks_no_hand_for_the_cluster(
    landing, capsys
):
    """Flux applies every k8s tag, so the landing asks nothing more and the changed set names
    them. Its line names no `deploy.sh` run, which applies nothing on the cluster or the Pi."""
    ln, calls = _broad_fallback(landing, Fakes(changed_rc=DEPLOY_BROAD))
    deploy.derive_from_diff(ln)
    assert ln.resolved_tags == []
    assert [c[1] for c in calls if c[0] == "deploy_tags"] == [("changed", "beefbeef")]
    out = capsys.readouterr().out
    assert "changed set names its k8s tags" in out
    assert "deploy.sh" not in out and "target=daniel-pi" not in out
