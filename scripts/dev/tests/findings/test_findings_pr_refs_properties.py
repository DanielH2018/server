"""`pr_refs` agrees with a token-level oracle over PR bodies built from closing fragments.

A fragment is a keyword in random case, an optional colon, whitespace, then a bare `#n`, an
`owner/repo#n` or an issue URL. Some fragments are spoiled so GitHub would not close on them: a
word glued in front of the keyword or after the number, a non-closing word, or a qualifier
naming another repository. The oracle reads the fragment list, never the joined text. GitHub
closes on `Fixes: #12` too: "Filed and not fixed: #2509" closed #2509, which
`deploy_tools/land_lib/closing_refs.py` records. A number is ASCII digits, as GitHub links no
other. The property tests run under the profile `tests/hypothesis_profiles.py` loads.

Run: uv run pytest scripts/dev/tests/findings/test_findings_pr_refs_properties.py
"""

from dataclasses import dataclass

from hypothesis import example, given
from hypothesis import strategies as st

from dev.findings_lib.issue_model import pr_refs

_CLOSING = [
    *["close", "closes", "closed", "fix", "fixes", "fixed"],
    *["resolve", "resolves", "resolved"],
]
_NOT_CLOSING = ["closure", "fixing", "resolving", "see", "issue"]
_REPOS = ["DanielH2018/server", "acme/tools"]
_OTHER_REPOS = ["DanielH2018/dotfiles", "SomeoneElse/server", "acme/tools2", "x/y"]
_NOISE = [" ", "\n", "\r\n", ". ", ", ", " and ", "; see #77 ", " (", ") "]


@dataclass(frozen=True)
class _Fragment:
    text: str
    closes: bool
    number: int


def _random_case(word: str, upper: list[bool]) -> str:
    return "".join(
        c.upper() if up else c.lower() for c, up in zip(word, upper, strict=True)
    )


@st.composite
def _fragment(draw, repo: str) -> _Fragment:
    closing = draw(st.booleans())
    word = draw(st.sampled_from(_CLOSING if closing else _NOT_CLOSING))
    keyword = _random_case(
        word, draw(st.lists(st.booleans(), min_size=len(word), max_size=len(word)))
    )
    number = draw(st.integers(1, 10**5))
    qualifier = draw(st.sampled_from([None, repo, repo.upper(), *_OTHER_REPOS]))
    as_url = qualifier is not None and draw(st.booleans())
    if qualifier is None:
        ref = f"#{number}"
    elif as_url:
        ref = f"https://github.com/{qualifier}/issues/{number}"
    else:
        ref = f"{qualifier}#{number}"
    glued_before = draw(st.sampled_from(["", "", "", "un", "pre"]))
    glued_after = draw(st.sampled_from(["", "", "", "x", "_2"]))
    colon = draw(st.sampled_from(["", ":"]))
    gap = draw(st.sampled_from([" ", "\t", "  ", "\n"]))
    closes = (
        closing
        and not glued_before
        and not glued_after
        and (qualifier is None or qualifier.lower() == repo.lower())
    )
    text = f"{glued_before}{keyword}{colon}{gap}{ref}{glued_after}"
    return _Fragment(text, closes, number)


@st.composite
def _bodies(draw, repo: str) -> tuple[list[str], set[int]]:
    """PR bodies built from fragments and noise, and the issue numbers the oracle says they close."""
    bodies, expected = [], set()
    for _ in range(draw(st.integers(0, 3))):
        fragments = draw(st.lists(_fragment(repo), max_size=5))
        parts = [draw(st.sampled_from(_NOISE))]
        for fragment in fragments:
            parts += [fragment.text, draw(st.sampled_from(_NOISE))]
            if fragment.closes:
                expected.add(fragment.number)
        bodies.append("".join(parts))
    return bodies, expected


@given(
    case=st.sampled_from(_REPOS).flatmap(
        lambda repo: st.tuples(st.just(repo), _bodies(repo))
    )
)
@example(
    case=(
        "DanielH2018/server",
        (["Fixes: #12", "Closes: DanielH2018/server#13", "Fixes #١"], {12, 13}),
    )
)
def test_pr_refs_matches_the_fragment_oracle(case):
    repo, (bodies, expected) = case
    assert pr_refs(bodies, repo) == expected
