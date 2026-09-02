"""Tests for scripts/docs/gen_reference_backlog.py: rendering only, gh is stubbed.

Run: uv run pytest scripts/docs/tests/test_gen_reference_backlog.py
"""

from __future__ import annotations

import build_docs
import gen_reference_backlog as g


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


def test_render_empty_says_so_instead_of_an_empty_table():
    md = g.render_markdown([])
    assert "No open findings" in md and "|---|" not in md


def test_main_writes_the_page_with_a_provenance_banner(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "load_issues", lambda state="open": [])
    out = tmp_path / "backlog.md"
    assert g.main(["--out", str(out)]) == 0
    text = out.read_text()
    assert text.startswith("---\ngenerated_from: scripts/docs/gen_reference_backlog.py")


def test_build_docs_registers_the_backlog_page():
    outs = [out for _argv, out in build_docs.GENERATORS]
    assert "docs/reference/backlog.md" in outs
