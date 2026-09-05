"""Tests for scripts/docs/reference/decisions.py.

Fixture-driven for the marker-extraction and rendering rules; a few tests read the real
tree to prove the non-vacuity claim CLAUDE.md's "A check that finds its own subject by
pattern" rule requires — see the two at the bottom.

Run: uv run pytest scripts/docs/tests/test_gen_reference_decisions.py
"""

from functools import cache
from docs.reference import decisions as g
from lib.repo_paths import REPO


def _write(tmp_path, rel_path: str, body: str):
    path = tmp_path / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def _rows(tmp_path):
    """build_rows against a non-repo fixture tree: git blame fails closed to "unknown"."""
    return g.build_rows(tmp_path, tmp_path)


def test_marker_line_renders_a_row(tmp_path):
    _write(tmp_path, "scripts/foo.py", "# DECIDED: keep it simple.\nx = 1\n")
    rows = _rows(tmp_path)
    assert len(rows) == 1
    assert rows[0]["text"].startswith("keep it simple.")
    assert rows[0]["path"] == "scripts/foo.py"
    assert rows[0]["line"] == "1"


def test_file_with_no_marker_renders_no_rows(tmp_path):
    """The red-proof pair to the row above: a sibling file, same tree, no marker text."""
    _write(tmp_path, "scripts/foo.py", "# DECIDED: keep it simple.\n")
    _write(tmp_path, "scripts/bar.py", "x = 1\ny = 2\n")
    rows = _rows(tmp_path)
    assert [r["path"] for r in rows] == ["scripts/foo.py"]


def test_continuation_lines_are_joined(tmp_path):
    _write(
        tmp_path,
        "scripts/foo.py",
        "# DECIDED: use option A.\n# Option B was slower in testing.\nx = 1\n",
    )
    rows = _rows(tmp_path)
    assert rows[0]["text"] == "use option A. Option B was slower in testing."


def test_continuation_stops_at_a_blank_comment_line(tmp_path):
    """A bare `#` line is the paragraph break a reader's eye uses — not a truly blank line."""
    _write(
        tmp_path,
        "scripts/foo.py",
        "# DECIDED: use option A.\n"
        "# More on option A.\n"
        "#\n"
        "# Unrelated commentary that should not be pulled in.\n",
    )
    rows = _rows(tmp_path)
    assert rows[0]["text"] == "use option A. More on option A."
    assert "Unrelated" not in rows[0]["text"]


def test_continuation_stops_at_a_non_comment_line(tmp_path):
    _write(
        tmp_path,
        "scripts/foo.py",
        "# DECIDED: use option A.\nx = 1  # not a continuation\n",
    )
    rows = _rows(tmp_path)
    assert rows[0]["text"] == "use option A."


def test_continuation_stops_before_a_second_marker(tmp_path):
    """Two markers in one comment block each get their own row, not each other's tail."""
    _write(
        tmp_path,
        "scripts/foo.py",
        "# DECIDED: first choice.\n"
        "# more about the first choice.\n"
        "# DECIDED: second choice.\n"
        "# more about the second choice.\n",
    )
    rows = {r["line"]: r["text"] for r in _rows(tmp_path)}
    assert rows["1"] == "first choice. more about the first choice."
    assert rows["3"] == "second choice. more about the second choice."


def test_prose_marker_has_no_continuation(tmp_path):
    """Plain prose (no comment prefix) is rendered as a single line, never extended."""
    _write(
        tmp_path,
        "docs/notes.md",
        "The reasoning is recorded as a `DECIDED:` marker at the code line.\n"
        "This next line is a separate sentence in the same paragraph.\n",
    )
    rows = _rows(tmp_path)
    assert len(rows) == 1
    assert "separate sentence" not in rows[0]["text"]


def test_first_sentence_used_for_the_short_field(tmp_path):
    _write(
        tmp_path,
        "scripts/foo.py",
        "# DECIDED: short version. Longer elaboration follows in the same clause.\n",
    )
    rows = _rows(tmp_path)
    assert rows[0]["first_sentence"] == "short version."


def test_planes_are_grouped_by_path_prefix(tmp_path):
    _write(tmp_path, "ansible/roles/k8s/foo/tasks/main.yml", "# DECIDED: k8s thing.\n")
    _write(tmp_path, "scripts/bar.py", "# DECIDED: scripts thing.\n")
    _write(tmp_path, "README.md", "A `DECIDED:` marker lives elsewhere.\n")
    rows = {r["path"]: r["plane"] for r in _rows(tmp_path)}
    assert rows["ansible/roles/k8s/foo/tasks/main.yml"] == "roles/k8s"
    assert rows["scripts/bar.py"] == "scripts"
    assert rows["README.md"] == "other"


def test_the_generators_own_source_and_test_file_are_excluded(tmp_path):
    """The generator's own docstring and this test's fixture strings both contain the
    literal marker text -- scanning them would render garbled rows and make the page
    count itself. See _SELF_PATHS.
    """
    _write(
        tmp_path,
        "scripts/docs/reference/decisions.py",
        '_MARKER = "DECIDED:"  # DECIDED: not a real project decision\n',
    )
    _write(
        tmp_path,
        "scripts/docs/tests/test_gen_reference_decisions.py",
        '"# DECIDED: fixture text, not a real decision"\n',
    )
    assert _rows(tmp_path) == []


def test_a_similarly_named_file_is_not_excluded(tmp_path):
    """The red-proof pair to the exclusion above: only the exact self-paths are skipped."""
    _write(
        tmp_path,
        "scripts/docs/reference/other.py",
        "# DECIDED: this one should still show up.\n",
    )
    rows = _rows(tmp_path)
    assert [r["path"] for r in rows] == ["scripts/docs/reference/other.py"]


def test_possible_duplicates_are_flagged(tmp_path):
    _write(tmp_path, "a.py", "# DECIDED: use the retry flag.\n")
    _write(tmp_path, "b.py", "# DECIDED: Use the retry flag.\n")
    rows = _rows(tmp_path)
    pairs = g.find_possible_duplicates(rows)
    assert len(pairs) == 1
    assert {p["path"] for p in pairs[0]} == {"a.py", "b.py"}


def test_distinct_markers_are_not_flagged_as_duplicates(tmp_path):
    """The red-proof pair to the duplicate-detection test above."""
    _write(tmp_path, "a.py", "# DECIDED: use the retry flag.\n")
    _write(tmp_path, "b.py", "# DECIDED: rotate the log file weekly.\n")
    rows = _rows(tmp_path)
    assert g.find_possible_duplicates(rows) == []


def test_blame_is_unknown_outside_a_git_repo(tmp_path):
    _write(tmp_path, "a.py", "# DECIDED: use the retry flag.\n")
    rows = g.build_rows(tmp_path, tmp_path)
    assert rows[0]["decided"] == "unknown"


def test_markdown_opens_with_the_provenance_banner(tmp_path):
    _write(tmp_path, "a.py", "# DECIDED: use the retry flag.\n")
    out = g.render_markdown(_rows(tmp_path))
    assert out.startswith("---\n")
    assert "generated_from: scripts/docs/reference/decisions.py" in out


def test_markdown_renders_the_duplicates_warning(tmp_path):
    _write(tmp_path, "a.py", "# DECIDED: use the retry flag.\n")
    _write(tmp_path, "b.py", "# DECIDED: Use the retry flag.\n")
    out = g.render_markdown(_rows(tmp_path))
    assert "Possible duplicates" in out
    assert "a.py:1" in out
    assert "b.py:1" in out


def test_markdown_omits_the_duplicates_warning_when_none_found(tmp_path):
    _write(tmp_path, "a.py", "# DECIDED: use the retry flag.\n")
    out = g.render_markdown(_rows(tmp_path))
    assert "Possible duplicates" not in out


def test_markdown_ends_with_exactly_one_newline(tmp_path):
    _write(tmp_path, "a.py", "# DECIDED: use the retry flag.\n")
    out = g.render_markdown(_rows(tmp_path))
    assert out.endswith("\n")
    assert not out.endswith("\n\n")


@cache
def _live_rows() -> tuple[dict, ...]:
    """The marker rows for the REAL tree, built once for this module.

    The three non-vacuity tests below each want the identical full-tree scan; without
    the cache each paid for its own. Rows are read-only here.
    """
    return tuple(g.build_rows(REPO, REPO))


def test_live_tree_yields_at_least_100_markers():
    """Non-vacuity: this repo carried 151+ markers when this page was written (2026-09-03).

    A pattern-matching check that finds nothing still passes trivially — see CLAUDE.md's
    "A check that finds its own subject by pattern" rule — so this asserts a real floor
    against the live tree rather than only against a fixture.
    """
    rows = _live_rows()
    assert len(rows) >= 100


def test_live_tree_includes_the_gitops_deploy_origin_slice_marker():
    """A named member the census must find, per the same CLAUDE.md rule.

    This is the marker CLAUDE.md's own "Review & Memory Hygiene" section cites as the
    worked example (`origin[:8]` is a fixed slice ... a MINIMUM width). It sits in
    `deploy_handlers.py`, beside `_rollback_k8s`, which is the code that makes the trade-off —
    a marker travels with that code rather than with the module it was written in.
    """
    rows = _live_rows()
    matches = [
        r
        for r in rows
        if r["path"] == "ansible/roles/setup/gitops_deploy/files/deploy_handlers.py"
        and "origin[:8]" in r["text"]
    ]
    assert matches, "expected the origin[:8] marker in deploy_handlers.py"


def test_live_tree_excludes_its_own_generator_and_test_file():
    """The self-exclusion applied against the real tree, not just a fixture."""
    rows = _live_rows()
    paths = {r["path"] for r in rows}
    assert "scripts/docs/reference/decisions.py" not in paths
    assert "scripts/docs/tests/test_gen_reference_decisions.py" not in paths
