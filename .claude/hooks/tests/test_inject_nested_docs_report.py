#!/usr/bin/env python3
"""Tests for `_inline_budget.inline_margins`, the arithmetic `_fits_inline` delegates to.

The limits come from the hook's own constants less its preamble, so a test here pins the
budget the hook actually applies: 7,272 characters and 188 newlines.

Run: uv run pytest .claude/hooks/tests/test_inject_nested_docs_report.py
"""

import test_inject_nested_docs as base

from _inline_budget import inline_margins

_mod = base._mod
MAX_CHARS = _mod.INLINE_MAX_CHARS - len(_mod._PREAMBLE)
MAX_LINES = _mod.INLINE_MAX_LINES - _mod._PREAMBLE.count("\n")
HEADER = _mod._header("ansible/roles/k8s/foo/CLAUDE.md", "ansible/roles/k8s/foo")


def _doc_with_block(chars, newlines):
    """A doc whose whole-doc block under `HEADER` is `chars` long with `newlines` newlines.

    The block adds the header, a newline after it and a closing newline, so the doc's own
    text carries two newlines fewer than the block.
    """
    body_chars = chars - len(HEADER) - 2
    lines = newlines - 2
    text = "x\n" * lines
    return text + "y" * (body_chars - len(text))


def test_a_block_at_both_limits_has_both_margins_at_zero_and_fits():
    text = _doc_with_block(7272, 188)
    assert (MAX_CHARS, MAX_LINES) == (7272, 188)
    assert inline_margins(text, HEADER, MAX_CHARS, MAX_LINES) == (0, 0)
    assert _mod._fits_inline(text, HEADER)


def test_one_char_over_gives_a_char_margin_of_minus_one_and_does_not_fit():
    text = _doc_with_block(7273, 188)
    assert inline_margins(text, HEADER, MAX_CHARS, MAX_LINES) == (-1, 0)
    assert not _mod._fits_inline(text, HEADER)
