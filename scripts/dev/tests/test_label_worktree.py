import subprocess

from label_worktree import main
from lib.git_testing import init_repo
from lib.worktree_label import read_label


def _tree(tmp_path, name="agent-a1"):
    return init_repo(tmp_path / ".claude" / "worktrees" / name)


def _titles(**by_number):
    def read(*argv, **_kw):
        return {"title": by_number[f"n{argv[2]}"]}

    return read


def test_issue_numbers_label_the_tree_with_the_first_title(tmp_path):
    tree = _tree(tmp_path)
    rc = main(["1345", "1386"], cwd=tree, read=_titles(n1345="Fix the cert watcher"))
    assert rc == 0
    assert read_label(tree) == "#1345 Fix the cert watcher (+#1386)"


def test_free_text_labels_the_tree_without_reading_github(tmp_path):
    tree = _tree(tmp_path, "bridge-cse_01")

    def no_read(*_a, **_k):
        raise AssertionError("--text must not call gh")

    assert main(["--text", "Tune the hallway lights"], cwd=tree, read=no_read) == 0
    assert read_label(tree) == "Tune the hallway lights"


def test_clear_removes_the_label(tmp_path):
    tree = _tree(tmp_path)
    main(["--text", "x"], cwd=tree)
    assert main(["--clear"], cwd=tree) == 0
    assert read_label(tree) is None


def test_a_checkout_outside_claude_worktrees_is_refused(tmp_path):
    # The primary checkout is the case this guards: a stray root file there parks the
    # GitOps deployer.
    primary = init_repo(tmp_path / "server")
    assert main(["--text", "x"], cwd=primary) == 64
    assert not (primary / ".claim").exists()


def test_a_failed_issue_read_writes_nothing(tmp_path):
    tree = _tree(tmp_path)

    def failing(*argv, **_kw):
        raise subprocess.CalledProcessError(1, ["gh", *argv])

    assert main(["1345"], cwd=tree, read=failing) == 1
    assert read_label(tree) is None
