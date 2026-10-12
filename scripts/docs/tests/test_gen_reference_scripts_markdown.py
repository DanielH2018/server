"""Tests for the page scripts/docs/reference/scripts.py renders: render_markdown's output.

Split from test_gen_reference_scripts.py, which holds the row-building and classification
tests, when that file reached its line cap. Fixture-driven: a synthetic scripts/ directory
under tmp_path, except the `live_page` tests, which render the real tree.

Run: uv run pytest scripts/docs/tests/test_gen_reference_scripts_markdown.py
"""

import re
import textwrap

import pytest

from docs.reference import scripts as g


def _write(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body))


@pytest.fixture(scope="module")
def live_page():
    """The page as the docs-refresh cron would write it from the tree at HEAD."""
    return g.render_markdown(g.build_rows())


@pytest.mark.parametrize(
    ("path", "test"),
    [
        ("scripts/deploy_tools/land_lib/ci.py", "test_land_ci.py"),
        ("scripts/diagnostics/probe_lib/health.py", "test_probe_health.py"),
        (
            "scripts/deploy_tools/runbook_gates_lib/k3s_upgrade.py",
            "test_runbook_gates_k3s_upgrade.py",
        ),
        (
            "scripts/deploy_tools/runbook_gates_lib/longhorn_dr.py",
            "test_runbook_gates_longhorn_dr.py",
        ),
        (
            "scripts/deploy_tools/runbook_gates_lib/longhorn_upgrade.py",
            "test_runbook_gates_longhorn_upgrade.py",
        ),
        (
            "scripts/deploy_tools/runbook_gates_lib/pinned_rotation.py",
            "test_runbook_gates_pinned_rotation.py",
        ),
    ],
)
def test_the_live_page_names_a_lib_modules_family_named_test(live_page, path, test):
    """A `*_lib` module's test is named after its family, not its stem alone (#4515)."""
    rows = [line for line in live_page.splitlines() if line.startswith(f"| `{path}` |")]
    assert rows, f"{path} has no row on the page; was it moved or renamed?"
    assert f"| `{test}` |" in rows[0]


def test_markdown_opens_with_the_provenance_banner(tmp_path):
    _write(tmp_path / "probe.py", '"""Summary."""\n')
    out = g.render_markdown(g.build_rows(tmp_path))
    assert out.startswith("---\n")
    assert "generated_from: scripts/docs/reference/scripts.py" in out


def test_the_tests_column_credits_only_a_test_named_after_the_script(tmp_path):
    """A suite elsewhere that imports the script is not its test (#3283)."""
    _write(tmp_path / "probe.py", '"""Summary."""\n')
    _write(tmp_path / "tested.py", '"""Summary."""\n')
    _write(tmp_path / "tests" / "test_tested.py", "import probe\nimport tested\n")
    out = g.render_markdown(g.build_rows(tmp_path))
    assert re.search(r"^\| `[^`]*/probe\.py` .*\| — \| — \|$", out, re.MULTILINE)
    assert re.search(
        r"^\| `[^`]*/tested\.py` .*\| `test_tested\.py` \|", out, re.MULTILINE
    )


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
