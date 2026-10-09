"""`primary_checkout_of` names the checkout a linked worktree hangs off, without starting a subprocess.

Run: uv run pytest scripts/lib/tests/test_repo_paths_primary_checkout.py
"""

from lib.repo_paths import primary_checkout_of


def test_a_primary_checkout_is_its_own_primary(tmp_path):
    (tmp_path / ".git").mkdir()
    assert primary_checkout_of(tmp_path) == tmp_path


def test_a_linked_worktree_resolves_to_the_checkout_it_hangs_off(tmp_path):
    primary = tmp_path / "server"
    wt = primary / ".claude" / "worktrees" / "feat"
    wt.mkdir(parents=True)
    gitdir = primary / ".git" / "worktrees" / "feat"
    gitdir.mkdir(parents=True)
    (wt / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
    assert primary_checkout_of(wt) == primary


def test_a_separated_metadata_dir_has_no_checkout_beside_it_and_answers_itself(
    tmp_path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").write_text(f"gitdir: {tmp_path / 'meta'}\n", encoding="utf-8")
    assert primary_checkout_of(repo) == repo


def test_a_tree_with_no_dotgit_entry_answers_itself(tmp_path):
    assert primary_checkout_of(tmp_path) == tmp_path
