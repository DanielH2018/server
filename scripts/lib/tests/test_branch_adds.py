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


def test_a_function_that_takes_or_returns_a_raw_dict_is_named(tmp_path):
    module = (
        "from typing import Any\n\nfrom lib.json_types import JsonValue\n\n\n"
        "def plain(name: str) -> str:\n    return name\n\n\n"
        "def takes(spec: dict) -> str:\n    return plain(spec['name'])\n\n\n"
        "class Reader:\n    def gives(self) -> dict[str, Any]:\n        return {}\n\n\n"
        "def narrows(value: JsonValue) -> str:\n    return takes(Reader().gives())\n"
    )
    repo = _branch(
        tmp_path, **{"scripts/rows.py": module, "scripts/use.py": "narrows\n"}
    )
    assert (added_advice(repo, "base") or "").splitlines()[-1] == (
        "  a raw dict in the signature. Parse the value into a typed one where it is "
        "read, as scripts/lib/k8s_objects.py and scripts/lib/container_entry.py do, "
        "and pass that on: scripts/rows.py:gives, scripts/rows.py:narrows, "
        "scripts/rows.py:takes"
    )


def test_a_raw_dict_signature_the_base_already_had_is_not_named(tmp_path):
    old = "def takes(spec: dict) -> str:\n    return spec['name']\n"
    repo = init_repo(tmp_path)
    commit(repo, "base", **{"scripts/rows.py": old})
    git(repo, "branch", "base")
    commit(repo, "move", **{"scripts/rows.py": None, "scripts/lib/rows.py": old})
    assert added_advice(repo, "base") == ""
