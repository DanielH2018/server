"""The line arm's text edits, `with_line` and `with_block`, over generated files.

A file is drawn as lines built from the marker strings and the characters a line split can
mistake for a line end (`\\r`, `\\x0c`, U+2028), each line ended by `\\n` or `\\r\\n`, with or
without a final one. An edit is kept only when `valid_edit` accepts it, as the manifest check
does. The property tests run under the profile `tests/hypothesis_profiles.py` loads.

Run: uv run pytest hosts/lib/tests/reconcile/test_reconcile_lines_properties.py
"""

import re

from hypothesis import assume, example, given
from hypothesis import strategies as st

from reconcile_lib.lines import valid_edit, with_block, with_line

MARKERS = ("# {mark} MANAGED", "{mark}", "# {mark}\x0c")
TOKENS = (
    "# BEGIN MANAGED",
    "# END MANAGED",
    "BEGIN",
    "END",
    "x=0",
    "x=1",
    "#",
    " ",
    "\r",
    "\x0c",
    " ",
)
PLAIN = tuple(t for t in TOKENS if "BEGIN" not in t and "END" not in t)


def lines_of(tokens: tuple[str, ...]) -> st.SearchStrategy[list[str]]:
    return st.lists(
        st.lists(st.sampled_from(tokens), max_size=3).map("".join), max_size=8
    )


@st.composite
def files(draw, tokens: tuple[str, ...] = TOKENS) -> str:
    return join(draw, draw(lines_of(tokens)))


def join(draw, lines: list[str]) -> str:
    ends = [draw(st.sampled_from(["\n", "\r\n"])) for _ in lines]
    text = "".join(line + end for line, end in zip(lines, ends, strict=True))
    return text.removesuffix(ends[-1]) if lines and draw(st.booleans()) else text


@st.composite
def block_edits(draw) -> dict:
    edit = {
        "path": "/etc/f",
        "marker": draw(st.sampled_from(MARKERS)),
        "block": draw(lines_of(TOKENS).map("\n".join)),
    }
    assume(valid_edit(edit))
    return edit


@st.composite
def line_edits(draw) -> dict:
    edit = {
        "path": "/etc/f",
        "line": draw(st.sampled_from(["x=1", "x=1\x0c", "# x=1", "x=1 y"])),
        "regexp": draw(st.sampled_from(["^x=", "x=1$", "^#", "1", "^x=1\\s*$"])),
    }
    assume(valid_edit(edit))
    return edit


def ends(edit: dict) -> tuple[str, str]:
    return tuple(edit["marker"].replace("{mark}", w) for w in ("BEGIN", "END"))


def split(text: str) -> list[str]:
    return re.findall(r"[^\n]*\n|[^\n]+\Z", text)


def bare(line: str) -> str:
    return line.removesuffix("\n").removesuffix("\r")


def wrapped(edit: dict) -> str:
    begin, end = ends(edit)
    return f"{begin}\n{edit['block'].rstrip(chr(10))}\n{end}\n"


CRLF_BLOCK = {"path": "/etc/f", "marker": "# {mark} MANAGED", "block": "x=1"}
CRLF_FILE = "a\r\n# BEGIN MANAGED\r\nx=0\r\n# END MANAGED\r\n"


def test_a_crlf_file_holding_the_block_keeps_exactly_one_begin_marker():
    out = with_block(CRLF_FILE, CRLF_BLOCK)

    assert out == "a\r\n# BEGIN MANAGED\nx=1\n# END MANAGED\n"


def test_a_line_anchored_at_its_end_replaces_its_crlf_copy():
    edit = {"path": "/etc/f", "line": "x=1", "regexp": r"^x=\d$"}

    assert with_line("x=0\r\ny=2\r\n", edit) == "x=1\ny=2\r\n"


def test_a_block_whose_body_holds_a_marker_line_is_refused():
    assert not valid_edit({**CRLF_BLOCK, "block": "x=1\n# END MANAGED\ny=2"})
    assert not valid_edit({**CRLF_BLOCK, "block": "# BEGIN MANAGED\r\nx=1"})


def test_a_marker_or_a_line_holding_a_carriage_return_is_refused():
    assert not valid_edit({**CRLF_BLOCK, "marker": "# {mark}\r"})
    assert not valid_edit({"path": "/etc/f", "line": "x=1\r", "regexp": "x"})


@given(files(PLAIN), files(PLAIN), block_edits(), st.data())
def test_a_file_holding_one_block_keeps_one_and_every_line_outside_it(
    head, tail, edit, data
):
    begin, end = ends(edit)
    held = data.draw(
        st.builds(
            lambda body: join(data.draw, [begin, *body, end]),
            lines_of(PLAIN),
        )
    )
    head += "\n" if head and not head.endswith("\n") else ""
    held += "\n" if tail and not held.endswith("\n") else ""

    out = with_block(head + held + tail, edit)

    assert out == head + wrapped(edit) + tail
    assert [bare(line) for line in split(out)].count(begin) == 1


@given(files(), block_edits())
@example(CRLF_FILE, CRLF_BLOCK)
@example("# BEGIN MANAGED\n", CRLF_BLOCK)
def test_with_block_splices_one_block_into_the_file_and_a_second_run_keeps_it(
    text, edit
):
    begin, end = ends(edit)
    lines = split(text)
    once = with_block(text, edit)
    spliced = {
        "".join(lines[:i]) + wrapped(edit) + "".join(lines[j:])
        for i in range(len(lines))
        for j in range(i + 1, len(lines) + 1)
        if bare(lines[i]) == begin and bare(lines[j - 1]) == end
    }
    appended = text + ("\n" if text and not text.endswith("\n") else "") + wrapped(edit)

    assert once in spliced | {appended}
    assert with_block(once, edit) == once


@given(files(), line_edits())
@example("x=0\r\n", {"path": "/etc/f", "line": "x=1", "regexp": "x=1$"})
def test_with_line_puts_the_line_last_among_its_matches_and_a_second_run_keeps_it(
    text, edit
):
    once = with_line(text, edit)
    found = [bare(line) for line in split(once)]
    hits = [line for line in found if re.search(edit["regexp"], line)]

    assert hits[-1] == edit["line"]
    assert with_line(once, edit) == once
