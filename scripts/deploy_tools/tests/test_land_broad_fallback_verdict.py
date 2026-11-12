"""The verdict a landing reaches when its diff fallback derives no tag.

Run: uv run pytest scripts/deploy_tools/tests/test_land_broad_fallback_verdict.py
"""

import pytest

from _land_fakes import MERGE_SHA, Fakes
from deploy_tools.land_lib import deploy
from deploy_tools.land_lib.outcome import Outcome
from deploy_tools.land_lib.landing import Classification


def test_a_fallback_deriving_no_tag_still_names_a_verdict(landing):
    """`--since` bounds a range wider than this PR. It waits on the promotion, and a changed
    set naming no tag ends it with a `VERDICT:` line."""
    ln, calls = landing(
        Fakes(changed="docs/landing.md\n", derived=([], "pr")), since="beefbeef"
    )
    ln.merge_sha = MERGE_SHA
    ln.ledger.t_ci = 2.0
    ln.needs_diff = True
    ln.classification = Classification()
    with pytest.raises(Outcome) as exc:
        deploy.deploy_phase(ln)
    assert (exc.value.verdict, exc.value.rc) == ("nothing-to-deploy", 0)
    assert "land_wait" in [c[0] for c in calls]
