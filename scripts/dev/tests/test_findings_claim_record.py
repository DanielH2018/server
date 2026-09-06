"""The claim record: what a claim comment says, and folding a comment list forward."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _findings_fakes import foreign_comment, operator_comment

from dev.findings_claim import _claim_age_days
from dev.findings_model import (
    LABELS,
    claim_comment,
    current_claim,
    is_operator_comment,
    release_comment,
)

WT = "worktree-issue-1132"


def _issue(*bodies):
    """An issue whose comments the operator wrote; pass a dict for any other author."""
    return {
        "number": 1,
        "comments": [b if isinstance(b, dict) else operator_comment(b) for b in bodies],
    }


# --- who may write a claim trailer: this repo is PUBLIC (#1280) -----------------------------


def test_a_claim_from_a_foreign_author_holds_nothing():
    """Denial of the backlog. A drive-by ``Claim:`` used to withhold the issue from `next`
    and make `claim` refuse it for as long as the named branch existed."""
    issue = _issue(foreign_comment(claim_comment("worktree-issue-1132", None, "t")))
    assert current_claim(issue) is None


def test_a_release_from_a_foreign_author_closes_nothing():
    """The double-assignment case. Branch names are public through PR heads, so a comment
    reading ``Released: `<branch>` `` used to hand a live claim's issue to a second session."""
    issue = _issue(
        claim_comment("worktree-issue-1132", None, "t"),
        foreign_comment(
            release_comment("worktree-issue-1132", "t", "not mine to release")
        ),
    )
    assert current_claim(issue) == "worktree-issue-1132"


def test_a_comment_with_no_author_metadata_holds_nothing():
    """FAIL-CLOSED, which the foreign-author tests above do not pin.

    Both of those carry `authorAssociation: NONE`; this one carries no author fields at all,
    the shape a fixture writes by hand. A comment nothing can attribute must not decide who
    holds an issue.
    """
    issue = {
        "number": 1,
        "comments": [{"body": claim_comment("worktree-issue-1132", None, "t")}],
    }
    assert current_claim(issue) is None


def test_the_operator_is_trusted_by_association_as_well_as_by_viewer():
    """Either signal suffices, so a claim posted from another of the operator's accounts —
    where `viewerDidAuthor` is False but the association still reads OWNER — still holds."""
    assert is_operator_comment({"authorAssociation": "OWNER"}) is True
    assert is_operator_comment({"viewerDidAuthor": True}) is True
    assert is_operator_comment({"authorAssociation": "NONE"}) is False
    assert is_operator_comment({}) is False


def test_both_new_labels_are_declared():
    assert "manual" in LABELS
    assert "claimed" in LABELS


def test_claim_comment_carries_a_parseable_trailer():
    text = claim_comment("worktree-issue-1132", "cse_01ABC", "2026-09-05T18:40Z")
    assert "Claim: `worktree-issue-1132`" in text
    assert "cse_01ABC" in text
    assert "2026-09-05T18:40Z" in text


def test_claim_comment_omits_the_session_when_there_is_none():
    text = claim_comment("worktree-issue-1132", None, "2026-09-05T18:40Z")
    assert "Claim: `worktree-issue-1132`" in text
    assert "session" not in text


def test_an_unclosed_claim_holds_the_issue():
    issue = _issue(claim_comment("worktree-issue-1132", None, "2026-09-05T18:40Z"))
    assert current_claim(issue) == "worktree-issue-1132"


def test_a_released_claim_holds_nothing():
    issue = _issue(
        claim_comment("worktree-issue-1132", None, "2026-09-05T18:40Z"),
        release_comment("worktree-issue-1132", "2026-09-05T21:02Z", "landed in #1270"),
    )
    assert current_claim(issue) is None


def test_a_claim_after_a_release_takes_the_issue():
    issue = _issue(
        claim_comment("worktree-issue-1132", None, "2026-09-05T18:40Z"),
        release_comment("worktree-issue-1132", "2026-09-05T19:00Z", None),
        claim_comment("worktree-issue-1140", None, "2026-09-05T19:30Z"),
    )
    assert current_claim(issue) == "worktree-issue-1140"


def test_the_FIRST_of_two_unreleased_claims_wins():
    # Two sessions racing. gh returns comments in createdAt order, so the earlier claim
    # is the earlier comment, and a later one does not silently steal the issue — which
    # is what lets the loser's own `release` still work.
    issue = _issue(
        claim_comment("worktree-issue-1132", None, "2026-09-05T18:40Z"),
        claim_comment("worktree-issue-1140", None, "2026-09-05T18:41Z"),
    )
    assert current_claim(issue) == "worktree-issue-1132"


def test_a_release_naming_a_different_worktree_does_not_close_the_claim():
    issue = _issue(
        claim_comment("worktree-issue-1132", None, "2026-09-05T18:40Z"),
        release_comment("worktree-issue-9999", "2026-09-05T19:00Z", None),
    )
    assert current_claim(issue) == "worktree-issue-1132"


def test_an_ordinary_comment_is_not_a_claim():
    issue = _issue("Re-observed by review-2026-09-04 (sighting 2).")
    assert current_claim(issue) is None


# --- one body carrying BOTH trailers: the claim wins (#1283) --------------------------------

_BOTH = f"Claim: `{WT}`\n\nReleased: `{WT}`\n"


def test_a_body_carrying_both_trailers_holds_the_claim():
    """Settles #1283's third mutation: `continue` in `current_claim`'s fold is load-bearing.

    Neither `claim_comment` nor `release_comment` ever emits both trailers, so a body with
    both is malformed input. Every fail-safe in this protocol resolves ambiguity by HOLDING —
    an unattributable comment folds into nothing, an unreadable worktree keeps its claim,
    `reap` refuses on a git error — because releasing on a guess hands live work to a second
    session. See the DECIDED marker at that `continue`.
    """
    assert current_claim(_issue(_BOTH)) == WT


def test_a_body_carrying_both_trailers_ages_the_claim_from_the_comment_that_opened_it():
    """`_claim_age_days` carries the identical fold and must make the identical choice.

    Its docstring says it has to skip exactly what `current_claim` skips, or a `claims` row
    ages a claim the register does not think exists. A `pass` there where `current_claim`
    has a `continue` is exactly that divergence, and it is invisible on a single comment —
    `claimed_at_comment` is already set by then and nothing clears it.

    So: the both-trailers body opens the episode, and a SECOND claim by the same worktree
    follows with no timestamp. Under the correct fold the second comment is skipped, because
    the claim is already open, and the age comes from the first. A `pass` releases at the
    first body, so the second comment reopens the episode and the age is read off a comment
    that carries no `createdAt` at all — None, for a claim `current_claim` says is held.
    """
    issue = _issue(
        operator_comment(_BOTH, createdAt="2026-09-01T00:00:00Z"),
        operator_comment(claim_comment(WT, None, "t")),
    )
    assert current_claim(issue) == WT
    assert _claim_age_days(issue, WT) is not None


def test_a_release_in_its_own_comment_still_closes_the_claim():
    """The rejecting half: the rule above is about ONE body, not about releases in general."""
    issue = _issue(claim_comment(WT, None, "t"), release_comment(WT, "t", None))
    assert current_claim(issue) is None
