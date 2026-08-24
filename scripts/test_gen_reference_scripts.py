"""Tests for scripts/gen_reference_scripts.py.

Fixture-driven: a synthetic scripts/ directory under tmp_path.

Run: uv run pytest scripts/test_gen_reference_scripts.py
"""

from __future__ import annotations

import textwrap

import gen_reference_scripts as g


def _write(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body))


def test_the_summary_is_the_docstrings_first_line(tmp_path):
    _write(
        tmp_path / "probe.py", '"""Read-only homelab diagnostics.\n\nMore prose.\n"""\n'
    )
    rows = {r["name"]: r for r in g.build_rows(tmp_path)}
    assert rows["probe.py"]["summary"] == "Read-only homelab diagnostics."


def test_a_script_is_never_imported_to_read_its_docstring(tmp_path):
    """Importing runs module-level code; ast.parse does not.

    A generator that imported its subjects would run 40 scripts' worth of top-level code
    on every docs refresh — including anything that dials a host or takes a lock.
    """
    _write(tmp_path / "boom.py", '"""Summary."""\nraise SystemExit("imported")\n')
    rows = {r["name"]: r for r in g.build_rows(tmp_path)}
    assert rows["boom.py"]["summary"] == "Summary."


def test_a_script_that_does_not_parse_is_reported_not_skipped(tmp_path):
    """Silently dropping it would make the page quietly incomplete."""
    _write(tmp_path / "broken.py", '"""Summary."""\ndef (\n')
    rows = {r["name"]: r for r in g.build_rows(tmp_path)}
    assert "broken.py" in rows
    assert "could not be parsed" in rows["broken.py"]["summary"]


def test_a_script_with_no_docstring_says_so(tmp_path):
    _write(tmp_path / "bare.py", "x = 1\n")
    rows = {r["name"]: r for r in g.build_rows(tmp_path)}
    assert "no module docstring" in rows["bare.py"]["summary"]


def test_test_files_and_private_modules_are_excluded(tmp_path):
    _write(tmp_path / "test_probe.py", '"""x"""\n')
    _write(tmp_path / "conftest.py", '"""x"""\n')
    _write(tmp_path / "_render_guard.py", '"""x"""\n')
    assert g.build_rows(tmp_path) == []


def test_shell_scripts_use_their_leading_comment_block(tmp_path):
    _write(
        tmp_path / "deploy.sh",
        "#!/usr/bin/env bash\n# Deploy a service under the git-tree lock.\n# More detail.\nset -e\n",
    )
    rows = {r["name"]: r for r in g.build_rows(tmp_path)}
    assert rows["deploy.sh"]["summary"] == "Deploy a service under the git-tree lock."


def test_the_usage_block_is_extracted_when_present(tmp_path):
    _write(
        tmp_path / "probe.py",
        '"""Summary.\n\nUsage::\n\n    uv run python scripts/probe.py targets\n"""\n',
    )
    rows = {r["name"]: r for r in g.build_rows(tmp_path)}
    assert "uv run python scripts/probe.py targets" in rows["probe.py"]["usage"]


def test_a_script_with_no_usage_block_has_an_empty_usage(tmp_path):
    _write(tmp_path / "probe.py", '"""Summary."""\n')
    rows = {r["name"]: r for r in g.build_rows(tmp_path)}
    assert rows["probe.py"]["usage"] == ""


def test_a_script_with_a_test_file_names_it(tmp_path):
    _write(tmp_path / "probe.py", '"""Summary."""\n')
    _write(tmp_path / "test_probe.py", '"""x"""\n')
    rows = {r["name"]: r for r in g.build_rows(tmp_path)}
    assert rows["probe.py"]["tests"] == "test_probe.py"


def test_a_script_with_no_test_file_is_reported_as_such(tmp_path):
    """An untested script is a fact worth surfacing, not an omission to hide."""
    _write(tmp_path / "probe.py", '"""Summary."""\n')
    rows = {r["name"]: r for r in g.build_rows(tmp_path)}
    assert rows["probe.py"]["tests"] == ""


def test_rows_are_sorted_by_name(tmp_path):
    for name in ("zeta.py", "alpha.py", "mid.py"):
        _write(tmp_path / name, '"""Summary."""\n')
    names = [r["name"] for r in g.build_rows(tmp_path)]
    assert names == sorted(names)


def test_markdown_opens_with_the_provenance_banner(tmp_path):
    _write(tmp_path / "probe.py", '"""Summary."""\n')
    out = g.render_markdown(g.build_rows(tmp_path))
    assert out.startswith("---\n")
    assert "generated_from: scripts/gen_reference_scripts.py" in out


def test_markdown_counts_the_untested_scripts(tmp_path):
    _write(tmp_path / "probe.py", '"""Summary."""\n')
    _write(tmp_path / "tested.py", '"""Summary."""\n')
    _write(tmp_path / "test_tested.py", '"""x"""\n')
    out = g.render_markdown(g.build_rows(tmp_path))
    assert "1 of 2" in out


def test_markdown_escapes_a_pipe_in_a_summary(tmp_path):
    """A summary is free text; a literal pipe would silently add a column."""
    _write(tmp_path / "p.py", '"""Reads a | b."""\n')
    out = g.render_markdown(g.build_rows(tmp_path))
    assert r"Reads a \| b." in out


def test_markdown_ends_with_exactly_one_newline(tmp_path):
    """A second newline makes end-of-file-fixer rewrite the page and abort the cron."""
    _write(tmp_path / "probe.py", '"""Summary."""\n')
    out = g.render_markdown(g.build_rows(tmp_path))
    assert out.endswith("\n")
    assert not out.endswith("\n\n")


def test_the_real_scripts_directory_yields_the_known_shape():
    """Guards the exclusion rules against the live tree, not just fixtures."""
    rows = g.build_rows()
    names = {r["name"] for r in rows}
    assert "probe.py" in names
    assert "deploy.sh" in names
    assert not any(n.startswith(("test_", "_")) for n in names)
    assert "conftest.py" not in names
    assert len(rows) >= 30
