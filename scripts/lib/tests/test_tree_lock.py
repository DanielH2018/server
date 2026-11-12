"""The git-tree lock's path has one definition per language, and the two agree.

Host templates render `server_git_tree_lock` from `group_vars/all.yml`; Python imports
`tree_lock.TREE_LOCK`. A drift between them splits the lock in two: the host crons and the
Python readers would each serialise against a different file (#3276).

Run: uv run pytest scripts/lib/tests/test_tree_lock.py
"""

from lib import yaml_fast
from lib.repo_paths import ALL_VARS
from lib.tree_lock import TREE_LOCK


def _group_var() -> str:
    return yaml_fast.safe_load(ALL_VARS.read_text())["server_git_tree_lock"]


def test_the_template_var_and_the_python_constant_agree_is_clean():
    assert _group_var() == TREE_LOCK
