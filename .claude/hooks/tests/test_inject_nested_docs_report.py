#!/usr/bin/env python3
"""Tests for `_inline_budget.inline_margins` and the hook's `--report` mode built on it.

The limits come from the hook's own constants less its preamble, so a test here pins the
budget the hook actually applies: 7,272 characters and 188 newlines.

The report tests build a fixture checkout under tmp_path through `lib.git_testing`, so the
caller's git env cannot redirect them at the real repository. The exit-status tests run the
hook as a subprocess, because the error swallow they guard against lives in its `__main__`.

Run: uv run pytest .claude/hooks/tests/test_inject_nested_docs_report.py
"""

import io
import os
import subprocess
import sys
from pathlib import Path

import test_inject_nested_docs as base
from lib.git_testing import git, init_repo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import _inline_budget as budget

# Reached through the hook module, which imported it from `_inline_budget`: the base module
# puts `.claude/hooks` on `sys.path` before it loads the hook.
_mod = base._mod
inline_margins = _mod.inline_margins
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


# ── the --report mode ────────────────────────────────────────────────────────────────

HOOK = os.path.join(base._HERE, "inject-nested-docs.py")


def _checkout(path, **files):
    """A git checkout at `path` tracking `files` (path → text) and a root `CLAUDE.md`."""
    init_repo(path)
    files.setdefault("CLAUDE.md", "# root\n" * 3)
    for name, text in files.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    git(path, "add", "-A")
    return path


def _rows(root):
    return {r.doc: r for r in budget.report_rows(str(root), MAX_CHARS, MAX_LINES)}


def _doc_with_chars_left(doc, chars_left):
    """A one-line doc at `doc` whose block under its directory trigger leaves `chars_left`."""
    head = budget.header(doc, os.path.dirname(doc))
    return "y" * (MAX_CHARS - chars_left - len(head) - 2)


def _run_report(root):
    return subprocess.run(
        [sys.executable, HOOK, "--report", "--root", str(root)],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=60,
    )


def test_doc_over_the_char_limit_reports_head_and_sorts_first(tmp_path):
    rows = _rows(
        _checkout(
            tmp_path,
            **{"a/CLAUDE.md": "x" * _mod.INLINE_MAX_CHARS, "b/CLAUDE.md": "short\n"},
        )
    )
    assert rows["a/CLAUDE.md"].form == "head"
    assert rows["a/CLAUDE.md"].chars_left < 0
    assert list(rows) == ["a/CLAUDE.md", "b/CLAUDE.md"]


def test_doc_under_the_budget_reports_whole(tmp_path):
    rows = _rows(_checkout(tmp_path, **{"a/CLAUDE.md": "short\n"}))
    assert rows["a/CLAUDE.md"].form == "whole"
    assert not rows["a/CLAUDE.md"].near


def test_doc_over_the_line_limit_reports_head(tmp_path):
    row = _rows(_checkout(tmp_path, **{"a/CLAUDE.md": "x\n" * 200}))["a/CLAUDE.md"]
    assert row.form == "head"
    assert row.lines_left < 0
    assert row.chars_left > 0


def test_doc_at_the_line_limit_reports_whole(tmp_path):
    # The block adds two newlines to the doc's 186 internal ones once rstrip drops the last.
    row = _rows(_checkout(tmp_path, **{"a/CLAUDE.md": "x\n" * 187}))["a/CLAUDE.md"]
    assert row.form == "whole"
    assert row.lines_left == 0


def test_margin_under_200_chars_is_marked_near(tmp_path):
    rows = _rows(
        _checkout(
            tmp_path,
            **{
                "a/CLAUDE.md": _doc_with_chars_left("a/CLAUDE.md", 199),
                "b/CLAUDE.md": _doc_with_chars_left("b/CLAUDE.md", 200),
            },
        )
    )
    assert (rows["a/CLAUDE.md"].chars_left, rows["a/CLAUDE.md"].near) == (199, True)
    assert (rows["b/CLAUDE.md"].chars_left, rows["b/CLAUDE.md"].near) == (200, False)


def test_margin_under_10_lines_is_marked_near(tmp_path):
    # A doc of n "x\n" lines makes a block of n + 1 newlines.
    rows = _rows(
        _checkout(tmp_path, **{"a/CLAUDE.md": "x\n" * 178, "b/CLAUDE.md": "x\n" * 177})
    )
    assert (rows["a/CLAUDE.md"].lines_left, rows["a/CLAUDE.md"].near) == (9, True)
    assert (rows["b/CLAUDE.md"].lines_left, rows["b/CLAUDE.md"].near) == (10, False)


def test_a_rule_matching_no_tracked_path_reports_none(tmp_path):
    rule = '---\npaths:\n  - "{}"\n---\n# rule\n'
    rows = _rows(
        _checkout(
            tmp_path,
            **{
                "a/CLAUDE.md": "short\n",
                ".claude/rules/lost.md": rule.format("nowhere/**"),
                ".claude/rules/found.md": rule.format("a/**"),
            },
        )
    )
    assert rows[".claude/rules/lost.md"].form == "none"
    assert rows[".claude/rules/found.md"].form == "whole"
    assert list(rows)[-1] == ".claude/rules/lost.md"


def test_root_claude_md_is_reported_by_lines_not_form(tmp_path):
    root = _checkout(tmp_path, **{"a/CLAUDE.md": "short\n", "CLAUDE.md": "r\n" * 250})
    out = io.StringIO()
    budget.print_report(
        str(root), _mod.INLINE_MAX_CHARS, _mod.INLINE_MAX_LINES, _mod._PREAMBLE, out
    )
    lines = out.getvalue().splitlines()
    assert lines[0].startswith("budgets: payload 7500 chars / 190 lines")
    assert "7272 chars / 188 newlines" in lines[0]
    assert lines[-1].startswith("root CLAUDE.md: 250 lines")
    assert not any(line.endswith(" CLAUDE.md") for line in lines[1:-1])


def test_report_on_a_fixture_checkout_exits_0(tmp_path):
    proc = _run_report(_checkout(tmp_path, **{"a/CLAUDE.md": "short\n"}))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines()[-1].startswith("root CLAUDE.md:")


def test_report_outside_a_git_checkout_exits_non_zero(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("# root\n")
    proc = _run_report(tmp_path)
    assert proc.returncode != 0
    assert "CalledProcessError" in proc.stderr


def test_report_with_no_claude_md_raises(tmp_path):
    proc = _run_report(_checkout(tmp_path))
    assert proc.returncode != 0
    assert "no tracked CLAUDE.md besides the root one" in proc.stderr


def test_report_on_the_live_tree_finds_its_named_members():
    rows = _rows(base._REPO)
    assert sum(doc.endswith("/CLAUDE.md") for doc in rows) >= 80
    assert "ansible/roles/k8s/traefik/CLAUDE.md" in rows
    assert ".claude/rules/python-layout.md" in rows
