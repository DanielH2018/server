from lib.worktree_label import (
    MAX_LABEL_CHARS,
    clear_label,
    has_generated_name,
    read_label,
    with_label,
    write_label,
)


def test_a_written_label_reads_back_and_is_shown_beside_the_name(tmp_path):
    write_label(tmp_path, "  #1345 Fix the cert watcher\n")
    assert read_label(tmp_path) == "#1345 Fix the cert watcher"
    assert with_label("agent-a1", tmp_path) == "agent-a1 [#1345 Fix the cert watcher]"


def test_a_tree_with_no_label_or_an_empty_one_shows_the_bare_name(tmp_path):
    assert with_label("agent-a1", tmp_path) == "agent-a1"
    (tmp_path / ".claim").write_text("\n\n")
    assert read_label(tmp_path) is None


def test_a_long_label_is_cut_to_the_banner_width(tmp_path):
    write_label(tmp_path, "x" * 200)
    label = read_label(tmp_path)
    assert label is not None
    assert len(label) == MAX_LABEL_CHARS


def test_clearing_removes_the_label_and_reports_whether_there_was_one(tmp_path):
    write_label(tmp_path, "#1 a")
    assert clear_label(tmp_path) is True
    assert read_label(tmp_path) is None
    assert clear_label(tmp_path) is False


def test_agent_and_bridge_trees_have_generated_names():
    assert has_generated_name("/r/.claude/worktrees/agent-a09d7a8b958b22321")
    assert has_generated_name(
        "/r/.claude/worktrees/bridge-cse_018nRsDCiAgNpbDiR1tDWJw3"
    )


def test_a_session_named_tree_is_not_generated():
    assert not has_generated_name("/r/.claude/worktrees/claim-labels")
    assert not has_generated_name("/r/.claude/worktrees/fanout-4298")
