"""The git-tree lock's path has one definition per language, and the two agree.

Host templates render `server_git_tree_lock` from `group_vars/all.yml`; Python imports
`deploy_locks.TREE_LOCK`. A drift between them splits the lock in two: the deployer's unit
and `deploy.sh` would each serialise against a different file, and a tick could rewrite the
tree under a snapshot (#3276).

Run: uv run pytest ansible/tests/deploy/test_tree_lock_single_definition.py
"""

from _helpers import REPO, load_yaml
from deploy_locks import TREE_LOCK


def _group_var() -> str:
    return load_yaml(REPO / "ansible/inventory/group_vars/all.yml")[
        "server_git_tree_lock"
    ]


def test_the_template_var_and_the_python_constant_agree_is_clean():
    assert _group_var() == TREE_LOCK
