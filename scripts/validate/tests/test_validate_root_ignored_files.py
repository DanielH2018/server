"""Each test builds a scratch git repository whose `.gitignore` opens with `/*`, like the real one."""

import re
import shutil
import sys
from pathlib import Path

from lib.git_testing import init_repo
from lib import proc_testing
from validate import root_ignored_files

GITIGNORE = "/*\n!/kept.md\n!/.gitignore\n.pytest_cache/\n"


def _repo(tmp_path, *names: str):
    init_repo(tmp_path)
    (tmp_path / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
    (tmp_path / "kept.md").write_text("tracked\n", encoding="utf-8")
    for name in names:
        if name.endswith("/"):
            (tmp_path / name).mkdir()
            (tmp_path / name / "x").write_text("x", encoding="utf-8")
        else:
            (tmp_path / name).write_text("x", encoding="utf-8")
    return tmp_path


def test_a_reincluded_or_local_only_root_entry_is_clean(tmp_path):
    repo = _repo(tmp_path, ".venv/", "ansible.log", ".fanout/")
    assert root_ignored_files.problems(repo) == []


def test_an_entry_ignored_by_a_more_specific_rule_is_clean(tmp_path):
    repo = _repo(tmp_path, ".pytest_cache/")
    assert root_ignored_files.problems(repo) == []


def test_a_new_root_file_hidden_by_the_deny_all_is_flagged(tmp_path):
    repo = _repo(tmp_path, "CONTRIBUTING.md", ".venv/")
    assert root_ignored_files.problems(repo) == ["CONTRIBUTING.md"]


def test_a_new_root_directory_hidden_by_the_deny_all_is_flagged(tmp_path):
    repo = _repo(tmp_path, "tools/")
    assert root_ignored_files.problems(repo) == ["tools"]


def test_the_real_gitignore_still_opens_with_the_deny_all():
    """The check keys on the literal `/*` line; if it changes, this check matches nothing."""
    gitignore = root_ignored_files.REPO / ".gitignore"
    assert root_ignored_files.DENY_ALL in gitignore.read_text().splitlines()


def test_main_prints_each_hidden_entry_as_a_fail_line_on_stderr(tmp_path):
    """`[FAIL] <path>: <reason>` on stderr, the shape every validator shares (#4480).

    The script finds its repo from its own location, so a copy of `scripts/lib` and
    `scripts/validate` runs
    unpatched in a scratch repository holding one hidden root file.
    """
    repo = _repo(tmp_path, "stray.txt")
    (repo / ".gitignore").write_text(GITIGNORE + "!/scripts/\n", encoding="utf-8")
    scripts = Path(root_ignored_files.__file__).resolve().parents[1]
    for package in ("lib", "validate"):
        shutil.copytree(
            scripts / package,
            repo / "scripts" / package,
            ignore=shutil.ignore_patterns("tests", "__pycache__"),
        )
    script = repo / "scripts/validate/root_ignored_files.py"

    run = proc_testing.run([sys.executable, str(script)])
    assert run.returncode == 1, run.stdout + run.stderr
    assert re.search(r"^\s*\[FAIL\] stray\.txt: \S", run.stderr, re.M), (
        run.stdout + run.stderr
    )
