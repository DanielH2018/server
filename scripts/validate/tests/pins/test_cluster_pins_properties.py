"""`code_part` and `assigns`, the cluster-pins line reader, checked against PyYAML.

A line is drawn as `key: <scalar> [# comment]`, the scalar emitted by PyYAML in each of its
styles, so PyYAML says what the line holds. The property tests run under the profile
`tests/hypothesis_profiles.py` loads.

Run: uv run pytest scripts/validate/tests/pins/test_cluster_pins_properties.py
"""

from dataclasses import dataclass

import yaml
from hypothesis import assume, example, given
from hypothesis import strategies as st
from lib import yaml_fast

from validate import cluster_pins

KEYS = (*cluster_pins.PINS, cluster_pins.K3S_KEY, cluster_pins.K3S_ACK_KEY)
# What the reader's quote and comment states turn on, which `st.text()` alone rarely strings together.
TOKENS = ("#", " #", ";", "==", '"', "'", "''", "\\", '\\"', " ", "\t", ":", ": ")
# YAML counts neither as a space, so a `#` after one starts no comment.
WIDE_SPACES = ("\xa0", "\u3000")
values = st.text() | st.lists(
    st.sampled_from(TOKENS + WIDE_SPACES) | st.text(max_size=3), max_size=10
).map("".join)
# PyYAML refuses a non-printable character even in a comment, and a line break would end the line.
comment_text = st.text(
    st.characters(
        exclude_categories=("Cc", "Cs", "Cn"), exclude_characters="\u2028\u2029\ufeff"
    ),
    max_size=10,
)


@dataclass(frozen=True)
class Line:
    """One `key: <scalar>` line, with the value PyYAML emitted the scalar from."""

    key: str
    value: str
    text: str


@st.composite
def lines(draw, comments: st.SearchStrategy[str]) -> Line:
    key, value = draw(st.sampled_from(KEYS)), draw(values)
    scalar = yaml.safe_dump(
        value,
        default_style=draw(st.sampled_from([None, '"', "'"])),
        allow_unicode=draw(st.booleans()),
        width=float("inf"),
    )
    scalar = scalar.removesuffix("\n").removesuffix("\n...")
    # A plain or single-quoted dump of a value holding a line break spans several lines.
    assume(not set(scalar) & set("\r\n\x85\u2028\u2029"))
    comment = draw(st.none() | comments)
    tail = "" if comment is None else f" # {comment}"
    return Line(key, value, f"{key}: {scalar}{tail}")


def example_line(scalar: str, value: str) -> Line:
    key = "k3s_version"
    return Line(key, value, f"{key}: {scalar} # c")


@given(lines(comment_text))
@example(example_line('"\\\\\\\\\\""', '\\\\"'))
@example(example_line("'x'''", "x'"))
@example(Line("k3s_version", "x", 'k3s_version: "x"#c'))
@example(example_line("a\xa0#b", "a\xa0#b"))
@example(example_line("a\u3000# b", "a\u3000# b"))
def test_the_code_part_of_a_line_loads_as_the_whole_line_does(line):
    parsed = yaml_fast.safe_load(line.text)

    assert parsed == {line.key: line.value}
    assert yaml_fast.safe_load(cluster_pins.code_part(line.text)) == parsed


@given(st.data(), st.sampled_from(KEYS))
def test_a_line_assigns_the_key_pyyaml_parses_and_not_one_its_comment_quotes(
    data, other
):
    quoting = comment_text.map(lambda text: f"{other}: {text}")
    line = data.draw(lines(comment_text | quoting))
    assume(other != line.key)
    # A value that quotes `other:` reads as an assignment: the reader cuts comments only and
    # errs toward a refusal.
    assume(other not in line.value)

    assert set(yaml_fast.safe_load(line.text)) == {line.key}
    assert cluster_pins.assigns(line.key, line.text)
    assert not cluster_pins.assigns(other, line.text)


@given(st.lists(values, min_size=1, max_size=3), st.sampled_from(KEYS))
@example(['x "y', '"z #w"'], "k3s_version")
@example(["x 'y", "'z #w'"], "k3s_version")
def test_a_flow_mapping_assigns_a_key_after_scalars_that_hold_quotes(scalars, key):
    flow = {f"k{n}": value for n, value in enumerate(scalars)} | {key: "v1"}
    text = yaml.safe_dump(
        flow, default_flow_style=True, sort_keys=False, width=float("inf")
    ).removesuffix("\n")
    assume(not set(text) & set("\r\n\x85  "))

    assert yaml_fast.safe_load(text) == flow
    assert cluster_pins.assigns(key, text)


@given(st.text() | values)
def test_the_line_reader_takes_any_text_and_only_ever_cuts_its_end(text):
    assert text.startswith(cluster_pins.code_part(text))
    cluster_pins.assigns(cluster_pins.K3S_KEY, text)
