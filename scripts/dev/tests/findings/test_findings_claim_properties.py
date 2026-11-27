"""`claim_of` names the model's holder after any interleaving of claims, releases and other comments.

The machine posts comments onto one issue. Each comment's `createdAt` comes from a monotone clock
that can stand still, so ties are common, and each stamp is spelled with `Z` or `+00:00`, with or
without a fraction. The model folds the comments in posting order: the first open claim wins, only
a release naming the holder clears it, and nothing a foreign author writes counts. The seed and
the example count come from `tests/hypothesis_profiles.py`.

Run: uv run pytest scripts/dev/tests/findings/test_findings_claim_properties.py
"""

from datetime import UTC, datetime, timedelta

from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

from dev.findings_lib.issue_model import (
    Comment,
    claim_comment,
    claim_of,
    release_comment,
)

_START = datetime(2026, 11, 26, 9, 0, tzinfo=UTC)
_WORKTREES = st.sampled_from(["worktree-a", "worktree-b", "worktree-c"])
_TRAILERS = [
    f"{verb}: `{name}`"
    for verb in ("Claim", "Released")
    for name in ("worktree-a", "worktree-b", "worktree-c")
]
_BREAKS = ["\n", "\r", "\r\n", "\x85", " ", " ", "`", "``\n"]
_SMUGGLED = st.builds(
    "".join,
    st.lists(st.sampled_from(_BREAKS + _TRAILERS) | st.text(max_size=4), max_size=6),
)
_FREE_TEXT = st.none() | st.text() | _SMUGGLED
_STEPS = st.sampled_from([timedelta(0), timedelta(seconds=0.25), timedelta(seconds=1)])
_SPELLINGS = st.tuples(st.booleans(), st.booleans())
_OPERATOR = st.sampled_from(
    [{"association": "OWNER"}, {"association": "MEMBER"}, {"viewer_did_author": True}]
)
_FOREIGN = st.sampled_from(["NONE", "CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", ""])


def _spell(when: datetime, zulu: bool, fraction: bool) -> str:
    """``when`` as gh might write it: `Z` or `+00:00`, a fraction only when asked or needed."""
    text = when.strftime("%Y-%m-%dT%H:%M:%S")
    if fraction or when.microsecond:
        text += f".{when.microsecond:06d}"
    return text + ("Z" if zulu else "+00:00")


class ClaimLifecycle(RuleBasedStateMachine):
    """One issue's comment thread, and who the protocol says holds it."""

    def __init__(self):
        super().__init__()
        self.comments: list[Comment] = []
        self.clock = _START
        self.holder: str | None = None
        self.opened_at: datetime | None = None

    def post(self, body: str, step: timedelta, spelling: tuple[bool, bool], **who):
        self.clock += step
        stamp = _spell(self.clock, *spelling)
        self.comments.append(Comment(body, created_at=stamp, **who))

    @rule(
        worktree=_WORKTREES,
        session=_FREE_TEXT,
        step=_STEPS,
        spelling=_SPELLINGS,
        who=_OPERATOR,
    )
    def claim(self, worktree, session, step, spelling, who):
        self.post(claim_comment(worktree, session, "t"), step, spelling, **who)
        if self.holder is None:
            self.holder, self.opened_at = worktree, self.clock

    @rule(
        worktree=_WORKTREES,
        reason=_FREE_TEXT,
        step=_STEPS,
        spelling=_SPELLINGS,
        who=_OPERATOR,
    )
    def release(self, worktree, reason, step, spelling, who):
        self.post(release_comment(worktree, "t", reason), step, spelling, **who)
        if worktree == self.holder:
            self.holder = self.opened_at = None

    @rule(
        body=st.one_of(
            st.builds(claim_comment, _WORKTREES, _FREE_TEXT, st.just("t")),
            st.builds(release_comment, _WORKTREES, st.just("t"), _FREE_TEXT),
            _SMUGGLED,
        ),
        association=_FOREIGN,
        step=_STEPS,
        spelling=_SPELLINGS,
    )
    def foreign(self, body, association, step, spelling):
        self.post(body, step, spelling, association=association)

    @rule(prose=st.text(), step=_STEPS, spelling=_SPELLINGS, who=_OPERATOR)
    def noise(self, prose, step, spelling, who):
        self.post("Note: " + " ".join(prose.splitlines()), step, spelling, **who)

    @invariant()
    def claim_of_names_the_models_holder(self):
        claim = claim_of(tuple(self.comments))
        assert (claim and claim.worktree) == self.holder
        assert (claim and claim.opened_at) == self.opened_at

    @precondition(
        lambda self: (
            len({datetime.fromisoformat(c.created_at) for c in self.comments})
            == len(self.comments)
        )
    )
    @rule(data=st.data())
    def a_reordering_reads_the_same_when_every_instant_differs(self, data):
        shuffled = data.draw(st.permutations(self.comments))
        assert claim_of(tuple(shuffled)) == claim_of(tuple(self.comments))


TestClaimLifecycle = ClaimLifecycle.TestCase
