"""Tests for the page scripts/docs/reference/scripts.py renders: render_markdown's output.

Split from test_gen_reference_scripts.py, which holds the row-building and classification
tests, when that file reached its line cap. Fixture-driven: a synthetic scripts/ directory
under tmp_path.

Run: uv run pytest scripts/docs/tests/test_gen_reference_scripts_markdown.py
"""

import re
import textwrap

from docs.reference import scripts as g


def _write(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body))


def test_markdown_opens_with_the_provenance_banner(tmp_path):
    _write(tmp_path / "probe.py", '"""Summary."""\n')
    out = g.render_markdown(g.build_rows(tmp_path))
    assert out.startswith("---\n")
    assert "generated_from: scripts/docs/reference/scripts.py" in out


def test_markdown_counts_the_untested_scripts(tmp_path):
    _write(tmp_path / "probe.py", '"""Summary."""\n')
    _write(tmp_path / "tested.py", '"""Summary."""\n')
    _write(tmp_path / "test_tested.py", '"""x"""\n')
    out = g.render_markdown(g.build_rows(tmp_path))
    assert "1 of all 2" in out


def test_markdown_escapes_a_pipe_in_a_summary(tmp_path):
    """A summary is free text; a literal pipe would silently add a column."""
    _write(tmp_path / "p.py", '"""Reads a | b."""\n')
    out = g.render_markdown(g.build_rows(tmp_path))
    assert r"Reads a \| b." in out


def test_the_directory_column_is_the_top_level_subdirectory(tmp_path):
    """The page's Directory filter groups `docs/reference/x.py` with `docs/y.py`."""
    _write(tmp_path / "docs" / "reference" / "deep.py", '"""Summary."""\n')
    _write(tmp_path / "loose.py", '"""Summary."""\n')
    rows = {r["name"]: r for r in g.build_rows(tmp_path)}
    assert rows["deep.py"]["directory"] == "docs"
    assert rows["loose.py"]["directory"] == g.TOP_LEVEL
    out = g.render_markdown(list(rows.values()))
    assert re.search(
        r"^\| `[^`]*/docs/reference/deep\.py` \| docs \|", out, re.MULTILINE
    )


def test_markdown_ends_with_exactly_one_newline(tmp_path):
    """A second newline makes end-of-file-fixer rewrite the page and abort the cron."""
    _write(tmp_path / "probe.py", '"""Summary."""\n')
    out = g.render_markdown(g.build_rows(tmp_path))
    assert out.endswith("\n")
    assert not out.endswith("\n\n")
