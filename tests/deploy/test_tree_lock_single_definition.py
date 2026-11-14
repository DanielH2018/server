"""Every host cron opens the git-tree lock through one macro, which renders its one path.

`scripts/lib/tests/test_tree_lock.py` holds that path, `server_git_tree_lock`, equal to the
Python readers' `TREE_LOCK` (#3276).

Run: uv run pytest tests/deploy/test_tree_lock_single_definition.py
"""

from lib.repo_paths import REPO


# The host crons that take the git-tree lock, and the one macro they all take it through (#3722).
# Named rather than discovered, so a caller that stops importing the macro fails here by name.
# The macro lives in the shared template directory, which the host renderer's loader also
# searches. The two callers ship in their host components since H7.10.
_LOCK_MACRO = REPO / "k8s/templates/git-tree-lock.j2"
_COMPONENTS = REPO / "hosts/components"
_LOCK_CALLERS = (
    _COMPONENTS / "docs-refresh/templates/docs-refresh.sh.j2",
    _COMPONENTS / "eval-sweep/templates/eval-run.sh.j2",
)
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
    templates = sorted(
        [*(REPO / "k8s/roles").rglob("*.j2"), *_COMPONENTS.rglob("*.j2")]
        + [*(REPO / "k8s/templates").rglob("*.j2")]
    )
    openers = [p for p in templates if _opens_the_tree_lock_by_hand(p.read_text())]
    assert openers == [_LOCK_MACRO], (
        "open the git-tree lock through git-tree-lock.j2's take_git_tree_lock, not by hand: "
        f"{[str(p.relative_to(REPO)) for p in openers]}"
    )


def test_every_lock_caller_imports_the_macro_with_context():
    # `with context` is load-bearing: without it server_git_tree_lock renders empty inside the
    # macro and the caller locks a different file from `homelab-checkout-sync`.
    for caller in _LOCK_CALLERS:
        name = caller.name
        source = caller.read_text()
        assert _IMPORT in source, (
            f"{name} no longer imports take_git_tree_lock with context"
        )
        assert "{{ take_git_tree_lock(" in source, (
            f"{name} imports the macro but never calls it"
        )
