"""The `--tighten` writer's rules, as clean/flagged pairs over the pure rewrite.

`scripts/dev/tighten_ratchets.py` is the writer and `_ratchet.tighten` is the pure half it
calls; this module covers that half. What the pairs have to prove is that the fixer tightens
and ONLY tightens: a shrunk file's entry falls with no hand edit, and a grown file's entry
survives verbatim so `Ratchet.violations` still fails on it.

Run: uv run pytest ansible/tests/repo/test_ratchet_tighten.py
"""

from _ratchet import cap_for, tighten
from _ratchet_census import LENGTHS

HEADER = "# Modules over their cap.\n#\n# Format: `<path> <max>`.\n"


def test_a_shrunk_module_has_its_entry_lowered():
    text = f"{HEADER}scripts/big.py 900\n"
    assert (
        tighten(text, {"scripts/big.py": 780}, cap_for)
        == f"{HEADER}scripts/big.py 780\n"
    )


def test_a_lowered_entry_leaves_the_ratchet_clean():
    """The shrink's whole point: the edit the author would have made by hand."""
    counts = {"scripts/big.py": 780}
    tightened = tighten(f"{HEADER}scripts/big.py 900\n", counts, cap_for)
    assert LENGTHS.violations(counts, {"scripts/big.py": 780}) == []
    assert "780" in tightened


def test_a_grown_module_keeps_its_entry_and_still_fails_the_ratchet():
    """A fixer that raised a bar would be the ratchet's opposite."""
    text = f"{HEADER}scripts/big.py 900\n"
    counts = {"scripts/big.py": 950}
    assert tighten(text, counts, cap_for) == text
    assert LENGTHS.violations(counts, {"scripts/big.py": 900})


def test_a_trailing_comment_on_an_untouched_entry_survives_verbatim():
    """An entry explaining itself must not lose that text on an unrelated commit."""
    text = f"{HEADER}scripts/big.py 900  # split pending, see #1234\n"
    assert tighten(text, {"scripts/big.py": 900}, cap_for) == text


def test_a_lowered_entry_keeps_its_trailing_comment():
    text = f"{HEADER}scripts/big.py 900  # split pending, see #1234\n"
    assert tighten(text, {"scripts/big.py": 780}, cap_for) == (
        f"{HEADER}scripts/big.py 780  # split pending, see #1234\n"
    )


def test_an_entry_for_a_file_that_is_gone_is_deleted():
    assert tighten(f"{HEADER}scripts/gone.py 900\n", {}, cap_for) == HEADER


def test_an_entry_for_a_module_back_under_its_cap_is_deleted():
    """The list records remaining work only, so finishing the work deletes the line."""
    text = f"{HEADER}scripts/big.py 900\n"
    assert tighten(text, {"scripts/big.py": 400}, cap_for) == HEADER


def test_comments_blank_lines_and_order_survive_the_rewrite():
    """A regenerated file would lose the header block and the list's sort order."""
    text = f"{HEADER}\nscripts/a.py 900\nscripts/b.py 800\n"
    counts = {"scripts/a.py": 890, "scripts/b.py": 800}
    assert (
        tighten(text, counts, cap_for)
        == f"{HEADER}\nscripts/a.py 890\nscripts/b.py 800\n"
    )


def test_a_test_module_is_measured_against_the_test_cap():
    """`cap_of` is the ratchet's own, so a 520-line test module is not read as under its cap."""
    text = f"{HEADER}scripts/tests/test_a.py 560\n"
    counts = {"scripts/tests/test_a.py": 520}
    assert tighten(text, counts, cap_for) == f"{HEADER}scripts/tests/test_a.py 520\n"
    assert tighten(text, {"scripts/tests/test_a.py": 480}, cap_for) == HEADER
