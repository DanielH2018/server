"""The claim record: what a claim comment says, and folding a comment list forward."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dev.findings_model import (
    LABELS,
    claim_comment,
    current_claim,
    release_comment,
)


def _issue(*bodies):
    return {"number": 1, "comments": [{"body": b} for b in bodies]}


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


def test_a_body_with_crlf_line_endings_still_parses():
    issue = _issue(
        claim_comment("worktree-issue-1132", None, "2026-09-05T18:40Z").replace(
            "\n", "\r\n"
        )
    )
    assert current_claim(issue) == "worktree-issue-1132"
