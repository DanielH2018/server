"""Tests for scripts/docs/reference/backlog.py: rendering only, gh is stubbed.

Run: uv run pytest scripts/docs/tests/test_gen_reference_backlog.py
"""

import build_docs
from docs.reference import backlog as g


def _row(number, severity="high", escalated=False, title="t", **kw):
    base = {
        "number": number,
        "title": title,
        "state": "OPEN",
        "severity": severity,
        "kind": "gap",
        "domain": "network",
        "escalated": escalated,
        "no_vetted_remediation": False,
        "verify_by": False,
        "claimed": None,
        "first_seen": "2026-08-15",
        "reobservations": 0,
        "url": f"https://github.com/o/r/issues/{number}",
    }
    base.update(kw)
    return base


def test_render_orders_escalated_high_before_plain_high_before_medium():
    md = g.render_markdown([_row(1, "medium"), _row(2), _row(3, escalated=True)])
    assert md.index("#3") < md.index("#2") < md.index("#1")


def test_render_escapes_a_pipe_in_the_title():
    md = g.render_markdown([_row(1, title="a | b")])
    assert "a \\| b" in md and "| a | b |" not in md


def test_render_marks_no_vetted_remediation():
    md = g.render_markdown([_row(1, no_vetted_remediation=True)])
    assert "no vetted remediation" in md


def test_render_marks_a_finding_carrying_a_verify_by():
    md = g.render_markdown([_row(1, verify_by=True)])
    row = next(line for line in md.splitlines() if line.startswith("| [#1]"))
    assert row.rstrip().endswith("| ✓ |")


def test_render_leaves_the_verify_by_cell_blank_without_one():
    md = g.render_markdown([_row(1, verify_by=False)])
    row = next(line for line in md.splitlines() if line.startswith("| [#1]"))
    assert row.rstrip().endswith("| - |")


def test_render_shows_the_claiming_worktree():
    # Anchored on the full trailing shape, not just a substring: this also pins the Claim
    # column BEFORE Verify-by, since "worktree-issue-1132" and "-" are distinguishable in
    # either order — a column swap changes which cell comes last.
    md = g.render_markdown([_row(1, claimed="worktree-issue-1132")])
    row = next(line for line in md.splitlines() if line.startswith("| [#1]"))
    assert row.rstrip().endswith("| worktree-issue-1132 | - |")


def test_render_leaves_the_claim_cell_blank_without_one():
    # verify_by=True gives the two trailing cells different values ("-" and "✓"), so a
    # column swap changes this exact ending — two matching "-" cells would not have.
    md = g.render_markdown([_row(1, claimed=None, verify_by=True)])
    row = next(line for line in md.splitlines() if line.startswith("| [#1]"))
    assert row.rstrip().endswith("| - | ✓ |")


def test_render_empty_says_so_instead_of_an_empty_table():
    md = g.render_markdown([])
    assert "No open findings" in md and "|---|" not in md


def test_main_writes_the_page_with_a_provenance_banner(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "load_issues", lambda state="open": [])
    out = tmp_path / "backlog.md"
    assert g.main(["--out", str(out)]) == 0
    text = out.read_text()
    assert text.startswith("---\ngenerated_from: scripts/docs/reference/backlog.py")


def test_build_docs_registers_the_backlog_page():
    outs = [out for _argv, out in build_docs.GENERATORS]
    assert "docs/reference/backlog.md" in outs
