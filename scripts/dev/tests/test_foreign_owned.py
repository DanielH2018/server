"""Tests for the scan that names the paths another uid owns, and the command that clears them.

A test cannot chown a file to root without sudo, so the rejecting half injects `owner`.

Run: uv run pytest scripts/dev/tests/test_foreign_owned.py
"""

import os
from pathlib import Path

from foreign_owned import foreign_owned_advice, foreign_owned_paths


def _venv_with_pycache(root: Path) -> tuple[Path, Path]:
    pycache = root / ".venv" / "cryptography" / "x509" / "__pycache__"
    pycache.mkdir(parents=True)
    (pycache / "base.cpython-314.pyc").write_text("")
    stray = root / ".venv" / "a b.cpython-314.pyc"
    stray.write_text("")
    (root / "README.md").write_text("")
    return pycache, stray


def _root_owns(*paths: Path):
    root_owned = {str(p) for p in paths}
    return lambda path: 0 if path in root_owned else os.getuid()


def test_a_tree_we_own_throughout_is_clean(tmp_path):
    _venv_with_pycache(tmp_path)
    assert foreign_owned_paths(str(tmp_path)) == []
    assert foreign_owned_advice(str(tmp_path)) == []


def test_a_root_owned_dir_is_flagged_but_not_its_contents(tmp_path):
    pycache, stray = _venv_with_pycache(tmp_path)
    owner = _root_owns(pycache, pycache / "base.cpython-314.pyc", stray)
    assert foreign_owned_paths(str(tmp_path), owner) == sorted(
        [str(pycache), str(stray)]
    )


def test_a_root_that_does_not_exist_is_clean():
    assert foreign_owned_paths("/nonexistent/worktree") == []


def test_the_advice_names_each_path_and_one_quoted_sudo_rm(tmp_path):
    pycache, stray = _venv_with_pycache(tmp_path)
    lines = foreign_owned_advice(str(tmp_path), _root_owns(pycache, stray))
    assert f"    {pycache}" in lines
    assert (
        lines[-1]
        == f"  → an operator clears them with: sudo rm -rf -- '{stray}' {pycache}"
    )
