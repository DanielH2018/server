"""_mkdocs_freshness: the footer's text, and which pages get one."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import _mkdocs_freshness as hook
from lib.doc_freshness import PageFreshness


def _page(src_uri: str):
    return SimpleNamespace(file=SimpleNamespace(src_uri=src_uri))


def _prime(dates: dict[str, str], tracked: set[str]) -> None:
    hook._state["dates"] = dates
    hook._state["tracked"] = tracked


def test_the_footer_names_the_moved_sources():
    fresh = PageFreshness(
        "docs/a.md",
        "2026-08-01",
        [("x.py", "2026-09-01"), ("y.py", "2026-07-01")],
        [("x.py", "2026-09-01")],
    )
    out = hook.footer(fresh, "reference/freshness.md")
    assert "Last content change: 2026-08-01." in out
    assert "names: 2, of which 1 changed since: `x.py` (2026-09-01)." in out
    assert "[freshness table](reference/freshness.md)" in out
    assert out.startswith("\n\n---\n\n")


def test_the_footer_says_when_nothing_moved():
    fresh = PageFreshness("docs/a.md", "2026-08-01", [("x.py", "2026-07-01")], [])
    assert "names: 1, none changed since." in hook.footer(
        fresh, "reference/freshness.md"
    )


def test_an_uncommitted_page_is_stamped_as_such():
    assert "not committed" in hook.footer(PageFreshness("docs/a.md", ""), "x.md")


def test_a_hand_written_page_gets_the_footer_with_a_depth_relative_link():
    _prime({"docs/adr/0011-x.md": "2026-08-01"}, {"docs/adr/0011-x.md"})
    out = hook.on_page_markdown("# A decision\n", _page("adr/0011-x.md"), None, None)
    assert out.startswith("# A decision\n\n---\n\n")
    assert "[freshness table](../reference/freshness.md)" in out


def test_a_top_level_page_links_the_table_directly():
    _prime({"docs/a.md": "2026-08-01"}, {"docs/a.md"})
    out = hook.on_page_markdown("# A\n", _page("a.md"), None, None)
    assert "[freshness table](reference/freshness.md)" in out


def test_a_generated_page_is_left_alone():
    _prime({}, set())
    text = "---\ngenerated_from: scripts/docs/x.py\n---\n\n# X\n"
    assert hook.on_page_markdown(text, _page("reference/x.md"), None, None) == text


def test_an_archived_page_is_left_alone():
    _prime({}, set())
    assert (
        hook.on_page_markdown("# old\n", _page("archive/old.md"), None, None)
        == "# old\n"
    )
