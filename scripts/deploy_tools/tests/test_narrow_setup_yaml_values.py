"""YAML values that `==` and `yaml.YAMLError` do not account for, at the narrowing's own parses.

Python reads `1`, `1.0` and `true` as equal, so a value edit between them is still an edit a
host sees (#7279). PyYAML's constructor raises a bare `ValueError` for a timestamp-shaped scalar
that is no real date, so each parse has to answer it the way it answers `yaml.YAMLError` (#7280).

`mapping_at` has its own issue, #7273, so no fixture here puts such a value in a vars file.

Run: uv run pytest scripts/deploy_tools/tests/test_narrow_setup_yaml_values.py
"""

import pytest

import narrow_setup
from deploy_tools.narrow_lib.foreign_keys import keys_reaching
from deploy_tools.narrow_lib.setup_index import RoleIndex, foreign_tags
from deploy_tools.narrow_lib.setup_playbook import play_role_includes, playbook_roles
from deploy_tools.narrow_lib.task_walk import (
    changed_task_tags,
    file_tags,
    runs_the_same,
)
from lib.exit_codes import NARROW_SETUP_NARROWED
from lib.narrow_git import CannotNarrow

from _setup_role_fixtures import ALPHA, PLAYBOOK, ROLE, build

NOT_A_DATE = "d: 2026-13-45\n"


def _answer(call, *args):
    """`call(*args)`, with an escaping `ValueError` reported as the test's failure."""
    try:
        return call(*args)
    except ValueError as exc:
        pytest.fail(f"{call.__name__} let a ValueError escape: {exc}")


def _assert_refuses(call, *args) -> None:
    with pytest.raises(CannotNarrow):
        _answer(call, *args)


def test_an_edit_from_one_to_true_does_not_run_the_same():
    before = "- command: x\n  retries: 1\n"
    assert runs_the_same(before, "- command: x\n  retries: true\n") is False


def test_an_edit_from_one_to_one_point_zero_does_not_run_the_same():
    before = "- command: x\n  retries: 1\n"
    assert runs_the_same(before, "- command: x\n  retries: 1.0\n") is False


def test_a_task_value_edit_from_one_to_true_narrows_to_the_files_tag(tmp_path, capsys):
    """`role_tags` skips a task file `runs_the_same` passes, so this edit needs its tag."""
    tree = build(tmp_path)
    task_file = f"{ROLE}/tasks/alpha.yml"
    with_retries = ALPHA.replace(
        "  tags: [alpha]\n", "  retries: 1\n  tags: [alpha]\n", 1
    )
    tree.write(task_file, with_retries)
    old = tree.commit("retries as a number")
    tree.write(task_file, with_retries.replace("retries: 1", "retries: true"))
    new = tree.commit("retries as a boolean")

    argv = ["demo", old, new, "--playbook", PLAYBOOK, "--repo", str(tree.root)]
    assert narrow_setup.main(argv) == NARROW_SETUP_NARROWED
    assert capsys.readouterr().out.strip() == "alpha"


def test_file_tags_refuses_a_value_that_is_no_real_date():
    _assert_refuses(file_tags, NOT_A_DATE)


def test_runs_the_same_reads_a_value_that_is_no_real_date_as_not_the_same():
    """Identical texts, because a text that does not parse is never the same."""
    assert _answer(runs_the_same, NOT_A_DATE, NOT_A_DATE) is False


def test_changed_task_tags_gives_no_answer_for_a_value_that_is_no_real_date():
    assert _answer(changed_task_tags, NOT_A_DATE, ALPHA) is None


def test_playbook_roles_is_empty_for_a_value_that_is_no_real_date():
    assert _answer(playbook_roles, NOT_A_DATE) == set()


def test_play_role_includes_is_empty_for_a_value_that_is_no_real_date():
    assert _answer(play_role_includes, NOT_A_DATE, "demo") == []


def test_foreign_tags_refuses_another_roles_task_file_holding_no_real_date(tmp_path):
    tree = build(tmp_path)
    tree.write("ansible/roles/setup/other/tasks/main.yml", NOT_A_DATE)
    ref = tree.commit("another role's task file")
    playbook = "- hosts: all\n  roles: [demo, other]\n"
    _assert_refuses(foreign_tags, "demo", playbook, ref, str(tree.root))


def test_keys_reaching_refuses_an_owner_task_file_holding_no_real_date(tmp_path):
    tree = build(tmp_path)
    tree.write(f"{ROLE}/tasks/gamma.yml", NOT_A_DATE)
    ref = tree.commit("an owner task file")
    _assert_refuses(
        keys_reaching,
        f"{ROLE}/defaults/main.yml",
        {"demo_alpha_mode"},
        ref,
        str(tree.root),
    )


# `RoleIndex.__init__` runs `file_tags` over every task file first, so a role whose file holds
# the value never gets as far as the next two parses. Each test swaps the text in afterwards.
def test_the_facts_read_refuses_a_task_file_holding_no_real_date(tmp_path):
    tree = build(tmp_path)
    index = RoleIndex("demo", "HEAD", str(tree.root))
    index.task_text["tasks/alpha.yml"] = NOT_A_DATE
    _assert_refuses(index._facts_set_by, "tasks/alpha.yml")


def test_the_reachability_walk_refuses_a_task_file_holding_no_real_date(tmp_path):
    tree = build(tmp_path)
    index = RoleIndex("demo", "HEAD", str(tree.root))
    index.task_text["tasks/alpha.yml"] = NOT_A_DATE
    _assert_refuses(index._reachable_from, "tasks/alpha.yml")


def test_runs_the_same_reads_a_recursive_alias_as_not_the_same():
    recursive = "- command: x\n  vars: &v {a: *v}\n"
    assert runs_the_same(recursive, recursive) is False
