"""gen_reference_freshness: the ranking and the table, from synthetic PageFreshness rows."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gen_reference_freshness as g  # noqa: E402
from lib.doc_freshness import PageFreshness  # noqa: E402

A = PageFreshness(
    "docs/a.md", "2026-08-01", [("x.py", "2026-09-01")], [("x.py", "2026-09-01")]
)
B = PageFreshness("docs/adr/b.md", "2026-07-01", [], [])
C = PageFreshness(
    "docs/c.md",
    "2026-08-15",
    [("y.py", "2026-09-01"), ("z.py", "2026-09-02")],
    [("y.py", "2026-09-01"), ("z.py", "2026-09-02")],
)


def test_most_moved_first_then_oldest():
    assert [p.page for p in g.ranked([A, B, C])] == [
        "docs/c.md",
        "docs/a.md",
        "docs/adr/b.md",
    ]


def test_the_table_links_each_page_relative_to_the_reference_directory():
    out = g.render_markdown([A, B, C])
    assert "| [a.md](../a.md) | 2026-08-01 | 1 | 1 | `x.py` (2026-09-01) |" in out
    assert "| [adr/b.md](../adr/b.md) | 2026-07-01 | 0 | 0 | — |" in out


def test_the_most_recently_moved_source_is_the_latest_by_date():
    assert "`z.py` (2026-09-02)" in g.render_markdown([C])


def test_the_page_carries_the_provenance_banner():
    out = g.render_markdown([A])
    assert out.startswith(
        "---\ngenerated_from: scripts/docs/gen_reference_freshness.py\n"
    )
    assert "3 hand-written page(s)" in g.render_markdown([A, B, C])
