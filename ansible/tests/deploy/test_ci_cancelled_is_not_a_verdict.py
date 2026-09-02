"""A `cancelled` check-run holds the CI verdict at pending, and the operator docs say so.

Two merges in quick succession cancel the first run, so a merge commit whose merge was
immediately followed by another reads `completed cancelled` forever. The post-merge procedure in
the repo CLAUDE.md tells an operator to poll that commit's own check-runs, so without this the
documented gate waits on a run that can never go green. Measured 2026-08-27: `449c46d2` (#497)
reads cancelled because #498 merged seconds later, and green arrived on `17d71879`.

`deploy_logic.py` already gets this right. What these guard is that it keeps getting it right,
and that the doc keeps naming the constant an operator would go read.
"""

from deploy_logic import _CI_NO_VERDICT_CONCLUSIONS, ci_verdict
from _helpers import REPO

REQUIRED = frozenset({"prek (lint + validate + tests + secrets)"})
CLAUDE_MD = (REPO / "CLAUDE.md").read_text()


def _run(conclusion, name="prek (lint + validate + tests + secrets)"):
    return {"name": name, "status": "completed", "conclusion": conclusion}


def test_a_cancelled_required_run_is_pending():
    """The accepting half: cancelled means no verdict for this SHA, so the tick defers."""
    assert ci_verdict([_run("cancelled")], REQUIRED) == "pending", (
        "a cancelled run must hold at pending — mapping it to fail pages on an ordinary "
        "back-to-back push, and mapping it to pass ships an unverified commit"
    )


def test_a_genuinely_failed_run_still_fails():
    """The rejecting half, without which the test above is satisfied by always returning pending."""
    assert ci_verdict([_run("failure")], REQUIRED) == "fail"


def test_the_other_no_verdict_conclusions_are_pending_too():
    for conclusion in ("stale", "skipped_by_concurrency", None):
        assert ci_verdict([_run(conclusion)], REQUIRED) == "pending", (
            f"{conclusion!r} is in _CI_NO_VERDICT_CONCLUSIONS but does not read as pending"
        )


def test_cancelled_is_declared_no_verdict():
    assert "cancelled" in _CI_NO_VERDICT_CONCLUSIONS


def test_the_operator_docs_name_the_constant():
    """The doc sends an operator to this constant by name; a rename must break the doc, not hide.

    Textual on purpose. The behavioural tests above cover the deployer, and this covers the
    half a reader acts on — the post-merge procedure they follow by hand.
    """
    assert "_CI_NO_VERDICT_CONCLUSIONS" in CLAUDE_MD, (
        "the post-merge CI gate in CLAUDE.md must keep naming the constant that decides this, "
        "or the operator has no way to check the rule still holds"
    )
    assert "cancelled" in CLAUDE_MD, (
        "the post-merge procedure must say what a cancelled conclusion means, or an operator "
        "polls a run that can never go green"
    )
