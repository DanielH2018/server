"""What `repo_guards.py` tells a branch it adds: the net counts, and the names nothing calls.

Run: uv run pytest scripts/lib/tests/test_branch_adds.py
"""

from lib.branch_adds import added_advice
from lib.git_testing import commit, git, init_repo

_CLI = "scripts/tool.py"
_OLD_CLI = "import argparse\n\n\ndef parse():\n    argparse.ArgumentParser()\n"


def _branch(tmp_path, **files):
    """A repo whose `base` branch holds a one-function CLI, with ``files`` committed on top."""
    repo = init_repo(tmp_path)
    commit(repo, "base", **{_CLI: _OLD_CLI, "docs/tool.md": "# Tool\n"})
    git(repo, "branch", "base")
    commit(repo, "change", **files)
    return repo


def test_the_count_line_nets_modules_flags_tests_and_lines_by_where_they_land(tmp_path):
    cli = (
        "import argparse\n\nfrom scripts.rows import rows\n\n\ndef main():\n"
        "    parser = argparse.ArgumentParser()\n"
        '    parser.add_argument("--all")\n    rows(parser)\n'
    )
    repo = _branch(
        tmp_path,
        **{
            _CLI: cli,
            "scripts/rows.py": "def rows(parser):\n    return []\n",
            "scripts/tests/test_rows.py": "def test_rows():\n    assert True\n",
            "docs/tool.md": "# Tool\n\nIt takes `--all`.\n",
        },
    )
    assert added_advice(repo, "base") == (
        "branch adds: 1 module, 1 flag, 1 test, 6 lines outside tests and docs, "
        "2 lines of tests. Each count is net of what the branch removes."
    )


def test_a_new_name_only_a_test_uses_is_listed_and_one_with_a_caller_is_not(tmp_path):
    module = "def used():\n    return 1\n\n\ndef unused():\n    return used()\n"
    repo = _branch(
        tmp_path,
        **{
            "scripts/rows.py": module,
            "scripts/tests/test_rows.py": "def test_it():\n    assert unused\n",
        },
    )
    assert (added_advice(repo, "base") or "").splitlines()[1:] == [
        "  no caller outside tests. Delete each, or say in the PR body what calls it: "
        "scripts/rows.py:unused"
    ]


def test_a_moved_module_and_a_docs_edit_add_nothing(tmp_path):
    repo = _branch(
        tmp_path,
        **{
            _CLI: None,
            "scripts/cli/tool.py": _OLD_CLI,
            "docs/tool.md": "# Tool\n\nNew.\n",
        },
    )
    assert added_advice(repo, "base") == ""
