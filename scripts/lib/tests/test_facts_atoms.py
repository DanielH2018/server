"""One hash rule per citation form, each with the edit that must NOT move it and the edit that must."""

import subprocess
import textwrap

import pytest

from facts.atoms import HASHED_FORMS, Ambiguous, backrefs, hash_atom
from facts.citations import Citation


def _write(repo, rel, text):
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(text))
    return p


PY = '''
    """Module doc."""
    LIMIT = 85


    def run(x):
        """Runs."""
        return x + LIMIT


    class Gate:
        """Gate doc."""

        def check(self):
            return True
'''


def test_path_file_hashes_its_content(tmp_path):
    _write(tmp_path, "a/b.txt", "one\n")
    c = Citation("path", "a/b.txt", "a/b.txt", "")
    h1 = hash_atom(c, tmp_path)
    _write(tmp_path, "a/b.txt", "two\n")
    assert h1 and h1 != hash_atom(c, tmp_path)


def test_path_missing_is_none(tmp_path):
    assert hash_atom(Citation("path", "nope.txt", "nope.txt", ""), tmp_path) is None


def test_directory_hashes_its_tracked_tree(tmp_path, monkeypatch):
    _write(tmp_path, "d/x.txt", "x\n")
    _write(tmp_path, "d/y.txt", "y\n")
    monkeypatch.setattr(
        "facts.atoms._tracked_under", lambda repo, rel: ["d/x.txt", "d/y.txt"]
    )
    c = Citation("path", "d/", "d/", "")
    h1 = hash_atom(c, tmp_path)
    _write(tmp_path, "d/y.txt", "changed\n")
    assert h1 != hash_atom(c, tmp_path)


def test_symlinked_member_does_not_enter_a_directory_hash(tmp_path):
    """Reading a symlink hashes its TARGET, which the citation does not name."""
    _write(tmp_path, "d/x.txt", "x\n")
    _write(tmp_path, "outside.txt", "one\n")
    (tmp_path / "d" / "link.txt").symlink_to(tmp_path / "outside.txt")
    env = {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "HOME": str(tmp_path),
        "PATH": "/usr/bin:/bin",
    }
    subprocess.run(
        ["git", "init", "-q", "-b", "master", str(tmp_path)], check=True, env=env
    )
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, env=env)
    c = Citation("path", "d/", "d/", "")
    h1 = hash_atom(c, tmp_path)
    _write(tmp_path, "outside.txt", "two\n")
    assert h1 == hash_atom(c, tmp_path)


def test_directory_without_slash_resolving_to_a_dir_is_none(tmp_path):
    (tmp_path / "d").mkdir()
    assert hash_atom(Citation("path", "d", "d", ""), tmp_path) is None


def test_symbol_docstring_edit_is_clean(tmp_path):
    _write(tmp_path, "m.py", PY)
    c = Citation("symbol", "m.py:run", "m.py", "run")
    h1 = hash_atom(c, tmp_path)
    _write(tmp_path, "m.py", PY.replace('"""Runs."""', '"""Runs, reworded."""'))
    assert h1 == hash_atom(c, tmp_path)


def test_symbol_body_edit_is_flagged(tmp_path):
    _write(tmp_path, "m.py", PY)
    c = Citation("symbol", "m.py:run", "m.py", "run")
    h1 = hash_atom(c, tmp_path)
    _write(tmp_path, "m.py", PY.replace("x + LIMIT", "x - LIMIT"))
    assert h1 != hash_atom(c, tmp_path)


def test_symbol_assignment_value_is_flagged(tmp_path):
    _write(tmp_path, "m.py", PY)
    c = Citation("symbol", "m.py:LIMIT", "m.py", "LIMIT")
    h1 = hash_atom(c, tmp_path)
    _write(tmp_path, "m.py", PY.replace("LIMIT = 85", "LIMIT = 90"))
    assert h1 != hash_atom(c, tmp_path)


def test_symbol_class_and_missing(tmp_path):
    _write(tmp_path, "m.py", PY)
    assert hash_atom(Citation("symbol", "m.py:Gate", "m.py", "Gate"), tmp_path)
    assert hash_atom(Citation("symbol", "m.py:nope", "m.py", "nope"), tmp_path) is None


def test_yaml_value_change_is_flagged_and_comment_is_clean(tmp_path):
    _write(tmp_path, "d.yml", "a:\n  b: 1  # note\nlist:\n  - name: x\n")
    c = Citation("yaml", "d.yml:a.b", "d.yml", "a.b")
    h1 = hash_atom(c, tmp_path)
    _write(tmp_path, "d.yml", "a:\n  b: 1  # reworded\nlist:\n  - name: x\n")
    assert h1 == hash_atom(c, tmp_path)
    _write(tmp_path, "d.yml", "a:\n  b: 2\nlist:\n  - name: x\n")
    assert h1 != hash_atom(c, tmp_path)


def test_yaml_list_index_and_missing_key(tmp_path):
    _write(tmp_path, "d.yml", "list:\n  - name: x\n")
    assert hash_atom(
        Citation("yaml", "d.yml:list.0.name", "d.yml", "list.0.name"), tmp_path
    )
    assert (
        hash_atom(
            Citation("yaml", "d.yml:list.1.name", "d.yml", "list.1.name"), tmp_path
        )
        is None
    )


TEST_PY = """
    def test_gate():
        # fact: kubectl-wait-available-returns-instantly
        # fact: CLAUDE.md#After a PR Merges
        assert True


    def test_other():
        assert True
"""


def test_test_node_hashes_and_collects_backrefs(tmp_path):
    _write(tmp_path, "t/test_x.py", TEST_PY)
    c = Citation("test", "t/test_x.py::test_gate", "t/test_x.py", "test_gate")
    assert hash_atom(c, tmp_path)
    assert backrefs(c, tmp_path) == frozenset(
        {"kubectl-wait-available-returns-instantly", "CLAUDE.md#After a PR Merges"}
    )


def test_test_node_without_backref_is_empty_and_missing_node_is_none(tmp_path):
    _write(tmp_path, "t/test_x.py", TEST_PY)
    other = Citation("test", "t/test_x.py::test_other", "t/test_x.py", "test_other")
    assert backrefs(other, tmp_path) == frozenset()
    gone = Citation("test", "t/test_x.py::test_gone", "t/test_x.py", "test_gone")
    assert hash_atom(gone, tmp_path) is None


def test_marker_hashes_its_line_and_moves_with_it(tmp_path):
    _write(
        tmp_path,
        "m.py",
        "x = 1\n# DECIDED: a fixed slice while volume names vary\ny = 2\n",
    )
    c = Citation("marker", "m.py:DECIDED: a fixed slice", "m.py", "a fixed slice")
    h1 = hash_atom(c, tmp_path)
    _write(
        tmp_path,
        "m.py",
        "x = 1\nz = 0\n# DECIDED: a fixed slice while volume names vary\n",
    )
    assert h1 == hash_atom(c, tmp_path)  # moving the line does not move the hash
    _write(tmp_path, "m.py", "# DECIDED: a fixed slice while volume names differ\n")
    assert h1 != hash_atom(c, tmp_path)


def test_marker_ambiguous_raises_and_missing_is_none(tmp_path):
    _write(tmp_path, "m.py", "# DECIDED: a\n# DECIDED: a b\n")
    with pytest.raises(Ambiguous):
        hash_atom(Citation("marker", "m.py:DECIDED: a", "m.py", "a"), tmp_path)
    assert (
        hash_atom(Citation("marker", "m.py:DECIDED: zzz", "m.py", "zzz"), tmp_path)
        is None
    )


def test_probe_is_never_hashed_here(tmp_path):
    assert (
        hash_atom(Citation("probe", "probe.py kuma-drift", "", "kuma-drift"), tmp_path)
        is None
    )
    assert "probe" not in HASHED_FORMS


def test_hashed_forms_census():
    assert HASHED_FORMS == frozenset({"path", "symbol", "yaml", "test", "marker"})


def test_path_outside_repo_is_none(tmp_path):
    (tmp_path / "repo").mkdir()
    (tmp_path / "outside.txt").write_text("secret\n")
    c = Citation("path", "../outside.txt", "../outside.txt", "")
    assert hash_atom(c, tmp_path / "repo") is None
