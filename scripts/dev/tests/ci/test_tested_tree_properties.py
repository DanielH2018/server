"""`batch_members` reads exactly one `Members:` line out of an untrusted pull-request body.

A body is random prose, with every line that opens `Members:` removed, around one or more
`Members:` lines built from distinct numbers, separators of spaces and tabs, optional zero
padding, optional trailing blanks, and LF or CRLF line endings. The property tests run
under the profile `tests/hypothesis_profiles.py` loads.

Run: uv run pytest scripts/dev/tests/ci/test_tested_tree_properties.py
"""

import re

from hypothesis import example, given
from hypothesis import strategies as st

from dev.tested_tree import MAX_MEMBERS, batch_members

_SEPARATORS = st.sampled_from([" ", "\t", "  "])
_ENDINGS = st.sampled_from(["\n", "\r\n"])
_TAILS = st.sampled_from(["", " ", "\t", "  "])
_PROSE = st.lists(
    st.text().map(
        lambda text: [
            line for line in text.split("\n") if not line.startswith("Members:")
        ]
    ),
    max_size=3,
)


@st.composite
def _members_line(draw, numbers: list[int]) -> str:
    parts = [
        draw(_SEPARATORS) + "#" + "0" * draw(st.integers(0, 2)) + str(n)
        for n in numbers
    ]
    return "Members:" + "".join(parts) + draw(_TAILS)


@st.composite
def _body(draw, members_lines: list[str]) -> str:
    """Prose lines with each of `members_lines` at a random position, joined by one ending.

    On an LF body a `Members:` line may still end in `\r`, as the last line of a CRLF paste does.
    """
    ending = draw(_ENDINGS)
    lines = [line for chunk in draw(_PROSE) for line in chunk]
    for members in members_lines:
        if ending == "\n":
            members += draw(st.sampled_from(["", "\r"]))
        lines.insert(draw(st.integers(0, len(lines))), members)
    return ending.join(lines)


_DISTINCT = st.lists(
    st.integers(0, 10**6), min_size=1, max_size=MAX_MEMBERS, unique=True
)


@given(
    body=st.text()
    | _DISTINCT.flatmap(lambda ns: _members_line(ns).flatmap(lambda m: _body([m])))
)
@example(body="Members: #١")
@example(body="Members: #\U0001d7cf #2")
def test_a_result_is_empty_or_the_numbers_of_one_members_line(body):
    """Whatever the body, the result is `[]` or the distinct ASCII numbers of a `Members:` line."""
    numbers = batch_members(body)
    if not numbers:
        return
    assert len(set(numbers)) == len(numbers) <= MAX_MEMBERS
    lines = [line for line in body.split("\n") if line.startswith("Members:")]
    assert any(
        [int(n) for n in re.findall(r"#([0-9]+)", line, re.ASCII)] == numbers
        for line in lines
    )


@given(data=st.data(), numbers=_DISTINCT)
def test_one_members_line_in_prose_reads_as_exactly_its_numbers(data, numbers):
    body = data.draw(_body([data.draw(_members_line(numbers))]))
    assert batch_members(body) == numbers


@given(data=st.data(), first=_DISTINCT, second=_DISTINCT)
def test_two_members_lines_name_no_batch(data, first, second):
    lines = [data.draw(_members_line(first)), data.draw(_members_line(second))]
    assert batch_members(data.draw(_body(lines))) == []


@given(data=st.data(), numbers=_DISTINCT, repeat=st.integers(0, MAX_MEMBERS - 1))
def test_a_repeated_number_names_no_batch(data, numbers, repeat):
    numbers = [*numbers, numbers[repeat % len(numbers)]]
    assert batch_members(data.draw(_body([data.draw(_members_line(numbers))]))) == []


def test_a_zero_padded_number_repeats_its_unpadded_form():
    assert batch_members("x\r\nMembers: #01 #1\r\ny") == []


@given(
    data=st.data(),
    numbers=st.lists(
        st.integers(0, 10**6),
        min_size=MAX_MEMBERS + 1,
        max_size=2 * MAX_MEMBERS,
        unique=True,
    ),
)
def test_more_than_max_members_numbers_name_no_batch(data, numbers):
    assert batch_members(data.draw(_body([data.draw(_members_line(numbers))]))) == []
