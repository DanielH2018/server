"""The git-tree lock's path has one definition per language, and the two agree.

Host templates render `server_git_tree_lock` from `group_vars/all.yml`; Python imports
`deploy_locks.TREE_LOCK`. A drift between them splits the lock in two: the deployer's unit
and `deploy.sh` would each serialise against a different file, and a tick could rewrite the
tree under a snapshot (#3276).

Run: uv run pytest ansible/tests/deploy/test_tree_lock_single_definition.py
"""

from _helpers import load_yaml
from deploy_locks import TREE_LOCK

from lib.repo_paths import ALL_VARS, REPO


def _group_var() -> str:
    return load_yaml(ALL_VARS)["server_git_tree_lock"]


def test_the_template_var_and_the_python_constant_agree_is_clean():
    assert _group_var() == TREE_LOCK


# The host crons that take the git-tree lock, and the one macro they all take it through (#3722).
# Named rather than discovered, so a caller that stops importing the macro fails here by name.
_INITIAL_SETUP_TEMPLATES = REPO / "ansible/roles/setup/initial_setup/templates"
_LOCK_MACRO = _INITIAL_SETUP_TEMPLATES / "git-tree-lock.j2"
_LOCK_CALLERS = ("secret-rotate.sh.j2", "docs-refresh.sh.j2", "eval-run.sh.j2")
_IMPORT = "{% from 'git-tree-lock.j2' import take_git_tree_lock with context %}"


def _opens_the_tree_lock_by_hand(source: str) -> bool:
    return "exec 9>{{ server_git_tree_lock }}" in source


def test_the_hand_written_lock_idiom_is_what_the_check_flags():
    # Red proof: the idiom every caller carried before #3722 is flagged, the macro call is not.
    assert _opens_the_tree_lock_by_hand(
        "exec 9>{{ server_git_tree_lock }}\nflock -w 3840 9 || exit 1\n"
    )
    assert not _opens_the_tree_lock_by_hand(
        _IMPORT + "\n{{ take_git_tree_lock('x') }}\n"
    )


def test_the_lock_macro_is_the_only_template_opening_the_tree_lock():
    templates = sorted((REPO / "ansible/roles").rglob("*.j2"))
    openers = [p for p in templates if _opens_the_tree_lock_by_hand(p.read_text())]
    assert openers == [_LOCK_MACRO], (
        "open the git-tree lock through git-tree-lock.j2's take_git_tree_lock, not by hand: "
        f"{[str(p.relative_to(REPO)) for p in openers]}"
    )


def test_every_lock_caller_imports_the_macro_with_context():
    # `with context` is load-bearing: without it server_git_tree_lock renders empty inside the
    # macro and the caller locks a different file from the deployer.
    for name in _LOCK_CALLERS:
        source = (_INITIAL_SETUP_TEMPLATES / name).read_text()
        assert _IMPORT in source, (
            f"{name} no longer imports take_git_tree_lock with context"
        )
        assert "{{ take_git_tree_lock(" in source, (
            f"{name} imports the macro but never calls it"
        )
