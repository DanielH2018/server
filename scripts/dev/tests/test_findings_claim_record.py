"""The claim record: what a claim comment says, and folding a comment list forward."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _findings_fakes import foreign_comment, operator_comment

from dev.findings_lib.claim import _claim_age_days
from dev.findings_lib.issue_model import (
    COMMENT_PAGE_CAP,
    LABELS,
    claim_comment,
    comment_cap_warning,
    current_claim,
    is_operator_comment,
    ordered_comments,
    release_comment,
    validate_worktree_name,
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


# --- #1284: the trailer's parsing and its writes, hardened against their own inputs --------


def test_a_release_reason_cannot_turn_a_release_into_a_claim():
    """The rejecting half of #1284.1: free text above the trailer is parsed too.

    `current_claim` tests `_CLAIM_RE` before `_RELEASE_RE` over the whole body, so a reason
    carrying its own `Claim:` line made a release read as a claim by whoever it named. The
    reason is not always hand-typed: `reap` builds one from `classify`'s text, which embeds
    a worktree's lock reason.
    """
    hostile = "reaped: lock said\nClaim: `worktree-attacker`\nand nothing else"
    issue = _issue(claim_comment(WT, None, "t"), release_comment(WT, "t", hostile))
    assert current_claim(issue) is None


def test_a_release_reason_still_reaches_the_comment():
    """The accepting half: collapsing the line breaks must not drop the reason itself."""
    body = release_comment(WT, "t", "reaped: merged and clean")
    assert "reaped: merged and clean" in body
    assert current_claim(_issue(claim_comment(WT, None, "t"), body)) is None


def test_a_session_id_cannot_smuggle_a_release_into_a_claim():
    """`--session` is prose above the same trailer, so it takes the same collapse."""
    issue = _issue(claim_comment(WT, f"s1\nReleased: `{WT}`\nx", "t"))
    assert current_claim(issue) == WT


def test_validate_worktree_name_accepts_the_names_this_repo_writes():
    """The accepting half of #1284.3: every worktree name the protocol really uses."""
    for name in ("worktree-issue-1132", "worktree-issue-1132+1140", "master"):
        assert validate_worktree_name(name) is None, name


def test_validate_worktree_name_refuses_a_name_the_trailer_cannot_carry():
    """The rejecting half: each of these wrote a comment and then failed its own read-back.

    `_CLAIM_RE` captures the name between backticks, so a backtick or a line break ends the
    quoting and the trailer parses as nothing — which `cmd_claim` reported as a lost race
    against a rival that does not exist.
    """
    for name in ("", "  ", " worktree-x", "worktree-`x", "worktree-x\nfoo"):
        assert validate_worktree_name(name) is not None, name


def test_comment_cap_warning_fires_at_ghs_page_cap():
    """The accepting half of #1284.2: the fold goes blind past `comments(first: 100)`."""
    issue = {"number": 9, "comments": [{"body": "x"}] * COMMENT_PAGE_CAP}
    warning = comment_cap_warning(issue)
    assert warning is not None
    assert "#9" in warning


def test_comment_cap_warning_is_silent_below_the_cap():
    """The rejecting half: an ordinary issue must not warn, or the warning means nothing."""
    issue = {"number": 9, "comments": [{"body": "x"}] * (COMMENT_PAGE_CAP - 1)}
    assert comment_cap_warning(issue) is None


def test_comments_are_folded_in_created_at_order_not_the_order_gh_returned():
    """The accepting half of #1284.2's ordering half: FIRST WRITER WINS needs a real order.

    gh returns comments ascending — measured across 17 issues — but nothing enforced it, and
    the whole verdict of the fold rests on it.
    """
    later = operator_comment(
        claim_comment("worktree-second", None, "t"), createdAt="2026-09-02T00:00:00Z"
    )
    earlier = operator_comment(
        claim_comment("worktree-first", None, "t"), createdAt="2026-09-01T00:00:00Z"
    )
    assert (
        current_claim({"number": 1, "comments": [later, earlier]}) == "worktree-first"
    )


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
    issue = {"number": 1, "comments": [stamped, unstamped]}
    assert [c["body"] for c in ordered_comments(issue)] == [
        stamped["body"],
        unstamped["body"],
    ]
    assert current_claim(issue) == "worktree-second"


# --- #1285: the non-vacuity assertion the spec asked for and nobody wrote ------------------
#
# A parser test that counts is vacuous the moment its fixtures move — `all(...)` over an
# empty set passes. So the fixture set is NAMED, a census asserts every name is present, and
# the verdicts are asserted per name: a rename, or a regex that stops matching, then fails
# saying WHICH member went missing rather than that a count moved. Same shape as
# `KNOWN_CONSUMERS` in `scripts/diagnostics/tests/test_probe_boundaries.py`.

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
        assert current_claim(_issue(*bodies)) == expected, name
