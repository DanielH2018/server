"""The shared task-file readers behave the way the guards built on them assume.

`walk_tasks` and `leaf_tasks` differ only over `block:`, and that difference is exactly what
made the pre-consolidation `_flatten` copies incompatible. A guard that got the wrong one still
passed — it just stopped seeing part of the tree — so the distinction needs its own coverage
rather than relying on the callers to notice.
"""

import pytest
from lib import yaml_fast

from _helpers import (
    ANSIBLE,
    K8S_ROLES,
    REPO,
    SETUP_ROLES,
    command_of,
    imported_task_files,
    imported_tasks,
    leaf_tasks,
    load_tasks,
    task_named,
    walk_tasks,
)

BLOCKED = yaml_fast.safe_load(
    """
- name: Plain first
  ansible.builtin.command: echo first
- name: Wrapper
  when: something
  block:
    - name: Inside block
      ansible.builtin.command: echo inside
  rescue:
    - name: Inside rescue
      ansible.builtin.shell:
        cmd: echo rescued
  always:
    - name: Inside always
      ansible.builtin.command:
        cmd: echo always
- name: Plain last
  ansible.builtin.command: echo last
"""
)


def _names(tasks):
    return [t.get("name") for t in tasks]


def test_walk_yields_the_wrapper_and_its_children():
    assert _names(walk_tasks(BLOCKED)) == [
        "Plain first",
        "Wrapper",
        "Inside block",
        "Inside rescue",
        "Inside always",
        "Plain last",
    ]


def test_leaf_drops_the_wrapper_and_keeps_run_order():
    assert _names(leaf_tasks(BLOCKED)) == [
        "Plain first",
        "Inside block",
        "Inside rescue",
        "Inside always",
        "Plain last",
    ]


def test_the_two_walks_disagree_only_about_wrappers():
    # The whole reason both exist. If this ever passes trivially, one of them has drifted into
    # the other and every ordering assertion downstream is off by the wrapper count.
    assert set(_names(walk_tasks(BLOCKED))) - set(_names(leaf_tasks(BLOCKED))) == {
        "Wrapper"
    }


def test_a_leaf_index_is_not_shifted_by_the_wrapper():
    leaves = leaf_tasks(BLOCKED)
    assert _names(leaves).index("Plain last") == 4
    assert _names(list(walk_tasks(BLOCKED))).index("Plain last") == 5


@pytest.mark.parametrize("walk", [walk_tasks, leaf_tasks], ids=["walk", "leaf"])
@pytest.mark.parametrize("empty", [None, []], ids=["none", "empty"])
def test_both_walks_tolerate_an_empty_tasks_file(walk, empty):
    assert list(walk(empty)) == []


@pytest.mark.parametrize("walk", [walk_tasks, leaf_tasks], ids=["walk", "leaf"])
def test_both_walks_skip_non_dict_entries(walk):
    assert list(walk([None, "a bare string", {"name": "real"}])) == [{"name": "real"}]


@pytest.mark.parametrize(
    "task, expected",
    [
        ({"ansible.builtin.command": "echo bare"}, "echo bare"),
        ({"ansible.builtin.command": {"cmd": "echo dict"}}, "echo dict"),
        ({"ansible.builtin.shell": "echo shell"}, "echo shell"),
        ({"ansible.builtin.shell": {"cmd": "echo shell dict"}}, "echo shell dict"),
        ({"ansible.builtin.command": {"argv": ["echo"]}}, ""),
        ({"ansible.builtin.copy": {"src": "x"}}, ""),
        ({"name": "no module at all"}, ""),
    ],
    ids=[
        "cmd-str",
        "cmd-dict",
        "shell-str",
        "shell-dict",
        "argv",
        "other-module",
        "none",
    ],
)
def test_command_of_reads_every_module_shape(task, expected):
    assert command_of(task) == expected


def test_task_named_descends_into_blocks():
    assert task_named(BLOCKED, "Inside rescue")["name"] == "Inside rescue"


@pytest.mark.parametrize(
    "fragment", ["Inside", "nothing matches this"], ids=["several", "none"]
)
def test_task_named_refuses_anything_but_one_match(fragment):
    with pytest.raises(AssertionError):
        task_named(BLOCKED, fragment)


def test_load_tasks_reads_a_real_role_and_returns_dicts():
    tasks = load_tasks(K8S_ROLES / "manifests" / "tasks" / "main.yml")
    assert tasks and all(isinstance(t, dict) for t in tasks)


def test_the_roots_point_at_the_real_tree():
    assert (REPO / "pyproject.toml").is_file()
    assert (ANSIBLE / "deploy.yml").is_file()
    assert K8S_ROLES.is_dir()


# ---- imported_tasks: an imports-only main.yml must expand, and an unimported sibling must not


def _role_with_imports(tmp_path):
    """A role whose main.yml is imports only, plus one sibling task file nothing imports."""
    tasks = tmp_path / "role" / "tasks"
    tasks.mkdir(parents=True)
    (tasks / "main.yml").write_text(
        "- name: First\n"
        "  ansible.builtin.import_tasks: first.yml\n"
        "- name: Second\n"
        "  ansible.builtin.import_tasks: second.yml\n"
        "  when: some_flag | bool\n"
    )
    (tasks / "first.yml").write_text(
        "- name: Wrapper\n"
        "  block:\n"
        "    - name: Inside first\n"
        "      ansible.builtin.command: echo first\n"
    )
    (tasks / "second.yml").write_text(
        "- name: Inside second\n  ansible.builtin.command: echo second\n"
    )
    (tasks / "agent.yml").write_text(
        "- name: Not imported\n  ansible.builtin.command: echo agent\n"
    )
    return tmp_path / "role"


def test_imported_tasks_expands_an_imports_only_main_in_import_order(tmp_path):
    """The red proof: `load_tasks` on the same main.yml yields two import entries and no
    command, which is the vacuous list the k3s readers exist to avoid."""
    role = _role_with_imports(tmp_path)
    assert _names(load_tasks(role / "tasks" / "main.yml")) == ["First", "Second"]
    assert _names(imported_tasks(role)) == ["Inside first", "Inside second"]
    assert [p.name for p in imported_task_files(role)] == ["first.yml", "second.yml"]


def test_imported_tasks_ignores_a_sibling_main_does_not_import(tmp_path):
    role = _role_with_imports(tmp_path)
    assert "Not imported" not in _names(imported_tasks(role))
    assert role / "tasks" / "agent.yml" not in imported_task_files(role)


def test_imported_tasks_keeps_a_non_import_entry_in_place(tmp_path):
    role = _role_with_imports(tmp_path)
    main = role / "tasks" / "main.yml"
    main.write_text(
        main.read_text() + "- name: Inline last\n  ansible.builtin.command: echo last\n"
    )
    assert _names(imported_tasks(role)) == [
        "Inside first",
        "Inside second",
        "Inline last",
    ]


def test_imported_tasks_reaches_the_k3s_role():
    # Non-vacuity against the real tree: the role the two folded readers are about.
    assert len(imported_task_files(SETUP_ROLES / "k3s")) >= 5
    assert imported_tasks(SETUP_ROLES / "k3s")
