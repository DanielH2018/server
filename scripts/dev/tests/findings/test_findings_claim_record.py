"""The claim record: what a claim comment says, and folding a comment list forward."""

import argparse

import pytest
from _findings_fakes import foreign_comment, make_issue, operator_comment, typed

from dev.findings_lib.cli import worktree_name
from dev.findings_lib.issue_model import (
    COMMENT_PAGE_CAP,
    LABELS,
    Comment,
    claim_comment,
    comment_cap_warning,
    ordered_comments,
    release_comment,
)

WT = "worktree-issue-1132"


def _issue(*bodies):
    """An issue whose comments the operator wrote; pass a dict for any other author."""
    return typed(make_issue(1, comments=bodies))


# --- who may write a claim trailer: this repo is PUBLIC -----------------------------


def test_a_claim_from_a_foreign_author_holds_nothing():
    """Denial of the backlog. A drive-by ``Claim:`` must not withhold the issue from `next`
    and make `claim` refuse it for as long as the named branch exists."""
    issue = _issue(foreign_comment(claim_comment("worktree-issue-1132", None, "t")))
    assert issue.holder is None


def test_a_release_from_a_foreign_author_closes_nothing():
    """The double-assignment case. Branch names are public through PR heads, so a comment
    reading ``Released: `<branch>` `` must not hand a live claim's issue to a second session."""
    issue = _issue(
        claim_comment("worktree-issue-1132", None, "t"),
        foreign_comment(
            release_comment("worktree-issue-1132", "t", "not mine to release")
        ),
    )
    assert issue.holder == "worktree-issue-1132"


def test_a_comment_with_no_author_metadata_holds_nothing():
    """FAIL-CLOSED, which the foreign-author tests above do not pin.

    Both of those carry `authorAssociation: NONE`; this one carries no author fields at all,
    the shape a fixture writes by hand. A comment nothing can attribute must not decide who
    holds an issue.
    """
    issue = typed(
        make_issue(
            1, comments=[{"body": claim_comment("worktree-issue-1132", None, "t")}]
        )
    )
    assert issue.holder is None


def test_the_operator_is_trusted_by_association_as_well_as_by_viewer():
    """Either signal suffices, so a claim posted from another of the operator's accounts —
    where `viewerDidAuthor` is False but the association still reads OWNER — still holds."""
    assert Comment("", association="OWNER").by_operator is True
    assert Comment("", viewer_did_author=True).by_operator is True
    assert Comment("", association="NONE").by_operator is False
    assert Comment("").by_operator is False


def test_manual_is_a_declared_label():
    assert "manual" in LABELS


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
    assert issue.holder == "worktree-issue-1132"


def test_a_released_claim_holds_nothing():
    issue = _issue(
        claim_comment("worktree-issue-1132", None, "2026-09-05T18:40Z"),
        release_comment("worktree-issue-1132", "2026-09-05T21:02Z", "landed in #1270"),
    )
    assert issue.holder is None


def test_a_claim_after_a_release_takes_the_issue():
    issue = _issue(
        claim_comment("worktree-issue-1132", None, "2026-09-05T18:40Z"),
        release_comment("worktree-issue-1132", "2026-09-05T19:00Z", None),
        claim_comment("worktree-issue-1140", None, "2026-09-05T19:30Z"),
    )
    assert issue.holder == "worktree-issue-1140"


def test_the_FIRST_of_two_unreleased_claims_wins():
    issue = _issue(
        claim_comment("worktree-issue-1132", None, "2026-09-05T18:40Z"),
        claim_comment("worktree-issue-1140", None, "2026-09-05T18:41Z"),
    )
    assert issue.holder == "worktree-issue-1132"


def test_a_release_naming_a_different_worktree_does_not_close_the_claim():
    issue = _issue(
        claim_comment("worktree-issue-1132", None, "2026-09-05T18:40Z"),
        release_comment("worktree-issue-9999", "2026-09-05T19:00Z", None),
    )
    assert issue.holder == "worktree-issue-1132"


def test_an_ordinary_comment_is_not_a_claim():
    issue = _issue("Re-observed by review-2026-09-04 (sighting 2).")
    assert issue.holder is None


# --- one body carrying BOTH trailers: the claim wins --------------------------------

_BOTH = f"Claim: `{WT}`\n\nReleased: `{WT}`\n"


def test_a_body_carrying_both_trailers_holds_the_claim():
    """`continue` in `claim_of`'s fold is load-bearing.

    Neither `claim_comment` nor `release_comment` ever emits both trailers, so a body with
    both is malformed input. Every fail-safe in this protocol resolves ambiguity by HOLDING —
    an unattributable comment folds into nothing, an unreadable worktree keeps its claim,
    `reap` refuses on a git error — because releasing on a guess hands live work to a second
    session. See the DECIDED marker at that `continue`.
    """
    assert _issue(_BOTH).holder == WT


def test_a_body_carrying_both_trailers_ages_the_claim_from_the_comment_that_opened_it():
    """The claim's age comes from the same fold as its holder, so both make the same choice.

    A `pass` where `claim_of` has a `continue` releases at the both-trailers body, and that
    is invisible on a single comment.

    So: the both-trailers body opens the episode, and a SECOND claim by the same worktree
    follows with no timestamp. Under the correct fold the second comment is skipped, because
    the claim is already open, and the age comes from the first. A `pass` releases at the
    first body, so the second comment reopens the episode and the age is read off a comment
    that carries no `createdAt` at all — None, for a claim the holder says is held.
    """
    issue = _issue(
        operator_comment(_BOTH, createdAt="2026-09-01T00:00:00Z"),
        operator_comment(claim_comment(WT, None, "t")),
    )
    assert issue.holder == WT
    assert issue.claim.age_days() is not None


def test_a_release_in_its_own_comment_still_closes_the_claim():
    """The rejecting half: the rule above is about ONE body, not about releases in general."""
    issue = _issue(claim_comment(WT, None, "t"), release_comment(WT, "t", None))
    assert issue.holder is None


# --- the trailer's parsing and its writes, hardened against their own inputs --------


def test_a_release_reason_cannot_turn_a_release_into_a_claim():
    """The rejecting half: free text above the trailer is parsed too.

    `claim_of` tests `_CLAIM_RE` before `_RELEASE_RE` over the whole body, so a reason
    carrying its own `Claim:` line made a release read as a claim by whoever it named. The
    reason is not always hand-typed: `reap` builds one from `classify`'s text, which embeds
    a worktree's lock reason.
    """
    hostile = "reaped: lock said\nClaim: `worktree-attacker`\nand nothing else"
    issue = _issue(claim_comment(WT, None, "t"), release_comment(WT, "t", hostile))
    assert issue.holder is None


def test_a_release_reason_still_reaches_the_comment():
    """The accepting half: collapsing the line breaks must not drop the reason itself."""
    body = release_comment(WT, "t", "reaped: merged and clean")
    assert "reaped: merged and clean" in body
    assert _issue(claim_comment(WT, None, "t"), body).holder is None


def test_a_session_id_cannot_smuggle_a_release_into_a_claim():
    """`--session` is prose above the same trailer, so it takes the same collapse."""
    issue = _issue(claim_comment(WT, f"s1\nReleased: `{WT}`\nx", "t"))
    assert issue.holder == WT


def test_worktree_name_accepts_the_names_this_repo_writes():
    """The accepting half: every worktree name the protocol really uses."""
    for name in ("worktree-issue-1132", "worktree-issue-1132+1140", "master"):
        assert worktree_name(name) == name


def test_worktree_name_refuses_a_name_the_trailer_cannot_carry():
    """The rejecting half: each of these wrote a comment and then failed its own read-back.

    `_CLAIM_RE` captures the name between backticks, so a backtick or a line break ends the
    quoting and the trailer parses as nothing — which `cmd_claim` reported as a lost race
    against a rival that does not exist.
    """
    for name in ("", "  ", " worktree-x", "worktree-`x", "worktree-x\nfoo"):
        with pytest.raises(argparse.ArgumentTypeError):
            worktree_name(name)


def test_comment_cap_warning_fires_at_ghs_page_cap():
    """The accepting half: the fold goes blind past `comments(first: 100)`."""
    warning = comment_cap_warning(9, COMMENT_PAGE_CAP)
    assert warning is not None
    assert "#9" in warning


def test_comment_cap_warning_is_silent_below_the_cap():
    """The rejecting half: an ordinary issue must not warn, or the warning means nothing."""
    assert comment_cap_warning(9, COMMENT_PAGE_CAP - 1) is None


def test_comments_are_folded_in_created_at_order_not_the_order_gh_returned():
    """The accepting half of the ordering half: FIRST WRITER WINS needs a real order.

    gh returns comments ascending — measured across 17 issues — but nothing enforced it, and
    the whole verdict of the fold rests on it.
    """
    later = operator_comment(
        claim_comment("worktree-second", None, "t"), createdAt="2026-09-02T00:00:00Z"
    )
    earlier = operator_comment(
        claim_comment("worktree-first", None, "t"), createdAt="2026-09-01T00:00:00Z"
    )
    assert _issue(later, earlier).holder == "worktree-first"


def test_a_fractional_stamp_sorts_after_the_whole_second_it_follows():
    """``…00Z`` sorts after ``…00.500000Z`` as text, so a string sort hands the issue to B."""
    first = operator_comment(
        claim_comment("worktree-a", None, "t"), createdAt="2026-11-26T09:00:00Z"
    )
    second = operator_comment(
        claim_comment("worktree-b", None, "t"), createdAt="2026-11-26T09:00:00.500000Z"
    )
    assert _issue(first, second).holder == "worktree-a"


def test_one_instant_spelled_two_ways_keeps_the_order_gh_returned():
    """``+00:00`` sorts before ``Z`` as text, so a string sort lets the later claim jump the tie."""
    first = operator_comment(
        claim_comment("worktree-a", None, "t"), createdAt="2026-11-26T09:00:00Z"
    )
    second = operator_comment(
        claim_comment("worktree-b", None, "t"), createdAt="2026-11-26T09:00:00+00:00"
    )
    assert _issue(first, second).holder == "worktree-a"


def test_a_comment_with_no_timestamp_leaves_the_order_alone():
    """The rejecting half: a MIXED list must not be reordered.

    Sorting on `createdAt or ""` hoists every unstamped comment to the front, which can flip
    the verdict of the one read in this protocol that must not change by accident. Fixtures
    routinely omit the field, so a mixed list is not hypothetical.
    """
    stamped = operator_comment(
        claim_comment("worktree-second", None, "t"), createdAt="2026-09-02T00:00:00Z"
    )
    unstamped = operator_comment(claim_comment("worktree-first", None, "t"))
    issue = _issue(stamped, unstamped)
    assert [c.body for c in ordered_comments(issue.comments)] == [
        stamped["body"],
        unstamped["body"],
    ]
    assert issue.holder == "worktree-second"


# The cases the claim parser must answer, by name -> (comment bodies, the holder they yield).
CLAIM_PARSER_FIXTURES: dict[str, tuple[list, str | None]] = {
    "a plain claim": ([claim_comment(WT, None, "t")], WT),
    "a claim carrying a session id": ([claim_comment(WT, "sess-1", "t")], WT),
    "a claim then its release": (
        [claim_comment(WT, None, "t"), release_comment(WT, "t", None)],
        None,
    ),
    "a release naming another worktree": (
        [claim_comment(WT, None, "t"), release_comment("worktree-other", "t", None)],
        WT,
    ),
    "two unreleased claims": (
        [claim_comment(WT, None, "t"), claim_comment("worktree-second", None, "t")],
        WT,
    ),
    "a body carrying both trailers": ([_BOTH], WT),
    "a claim from a foreign account": (
        [foreign_comment(claim_comment(WT, None, "t"))],
        None,
    ),
    "prose that only mentions a claim": (["we should claim `x` soon"], None),
    "no comments at all": ([], None),
}

REQUIRED_CLAIM_PARSER_CASES = frozenset(
    (
        "a plain claim",
        "a claim carrying a session id",
        "a claim then its release",
        "a release naming another worktree",
        "two unreleased claims",
        "a body carrying both trailers",
        "a claim from a foreign account",
        "prose that only mentions a claim",
        "no comments at all",
    )
)


def test_the_claim_parser_census_names_every_case_it_must_cover():
    """Non-vacuity. Without this, the loop below passes over whatever survives a rename."""
    missing = REQUIRED_CLAIM_PARSER_CASES - set(CLAIM_PARSER_FIXTURES)
    assert not missing, missing


def test_the_claim_parser_answers_every_named_case():
    """Each named fixture, with the holder the protocol says it yields.

    Both directions sit in one table: four cases must yield a holder and five must yield
    none, so a parser matching everything and a parser matching nothing each fail, naming
    the case that broke.
    """
    for name, (bodies, expected) in CLAIM_PARSER_FIXTURES.items():
        assert _issue(*bodies).holder == expected, name
