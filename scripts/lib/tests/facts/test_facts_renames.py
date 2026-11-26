"""The rename fixer: `move` rewrites a moved path in docs and comments, and nowhere else."""

import pytest

from lib.facts.renames import move
from lib.git_testing import commit, git, init_repo

_OLD, _NEW = "old/tool.py", "new/tool.py"


def _repo(tmp_path, **files):
    """A repo that tracks `new/tool.py` and `new/a.py`, and each of `files`."""
    repo = init_repo(tmp_path / "repo")
    commit(repo, "tree", **{_NEW: "x = 1\n", "new/a.py": "y = 1\n", **files})
    return repo


def test_a_moved_file_is_renamed_in_an_instruction_file_a_live_doc_and_a_comment(
    tmp_path,
):
    tasks = "kuma/tasks.yml"
    repo = _repo(
        tmp_path,
        **{
            "CLAUDE.md": "## Run\nRun `old/tool.py`.\n",
            "docs/run.md": "## Run\nSee `old/tool.py:main`.\n",
            tasks: "# See old/tool.py for the list.\npath: old/tool.py\n",
            "docs/archive/then.md": "It was `old/tool.py`.\n",
        },
    )
    moved = move(repo, _OLD, _NEW)
    assert moved.rewritten == {"CLAUDE.md": 1, "docs/run.md": 1, tasks: 1}
    assert (repo / "docs/run.md").read_text() == "## Run\nSee `new/tool.py:main`.\n"
    assert (repo / tasks).read_text() == (
        "# See new/tool.py for the list.\npath: old/tool.py\n"
    )
    assert (repo / "docs/archive/then.md").read_text() == "It was `old/tool.py`.\n"


def test_history_and_generated_text_keep_the_old_path(tmp_path):
    kept = (
        "**HISTORY — it lived at `old/tool.py` until the move.\n\n"
        "<!-- generated_from: x -->\n`old/tool.py`\n<!-- /generated_from -->\n\n"
    )
    repo = _repo(tmp_path, **{"CLAUDE.md": kept + "Run `old/tool.py`.\n"})
    assert move(repo, _OLD, _NEW).rewritten == {"CLAUDE.md": 1}
    assert (repo / "CLAUDE.md").read_text() == kept + "Run `new/tool.py`.\n"


def test_a_longer_path_or_name_that_holds_the_old_one_is_clean(tmp_path):
    text = "`x/old/tool.py` and `old/tool.py.bak` are other files.\n"
    repo = _repo(tmp_path, **{"CLAUDE.md": text})
    moved = move(repo, _OLD, _NEW)
    assert (moved.rewritten, moved.left) == ({}, {})
    assert (repo / "CLAUDE.md").read_text() == text


def test_a_directory_move_leaves_a_reference_whose_new_path_is_not_tracked(tmp_path):
    repo = _repo(tmp_path, **{"CLAUDE.md": "`old/a.py`, `old/b.py` and `old/`.\n"})
    moved = move(repo, "old/", "new/")
    assert moved.rewritten == {"CLAUDE.md": 2}
    assert moved.left == {"CLAUDE.md": ["old/b.py"]}
    assert (repo / "CLAUDE.md").read_text() == "`new/a.py`, `old/b.py` and `new/`.\n"


def test_a_move_to_a_path_the_tree_does_not_track_is_refused(tmp_path):
    repo = _repo(tmp_path)
    with pytest.raises(ValueError, match="nowhere is not tracked"):
        move(repo, "old", "nowhere")


def test_a_store_root_is_rewritten_in_place_of_the_repo(tmp_path):
    """The store links by the main checkout's path, and the session runs in a worktree."""
    repo = _repo(tmp_path, **{"CLAUDE.md": "Run `old/tool.py`.\n"})
    worktree = tmp_path / "worktree"
    git(repo, "worktree", "add", "-q", str(worktree))
    store = tmp_path / "store"
    (store / "archive").mkdir(parents=True)
    (store / "note.md").write_text(f"[tool]({repo}/old/tool.py) is `old/tool.py`.\n")
    (store / "archive" / "then.md").write_text("It was `old/tool.py`.\n")
    assert move(worktree, _OLD, _NEW, root=store).rewritten == {"note.md": 2}
    assert (store / "note.md").read_text() == (
        f"[tool]({repo}/new/tool.py) is `new/tool.py`.\n"
    )
    assert (store / "archive" / "then.md").read_text() == "It was `old/tool.py`.\n"
    assert (repo / "CLAUDE.md").read_text() == "Run `old/tool.py`.\n"


def test_a_dry_run_counts_and_writes_nothing(tmp_path):
    repo = _repo(tmp_path, **{"CLAUDE.md": "Run `old/tool.py`.\n"})
    assert move(repo, _OLD, _NEW, write=False).rewritten == {"CLAUDE.md": 1}
    assert (repo / "CLAUDE.md").read_text() == "Run `old/tool.py`.\n"
