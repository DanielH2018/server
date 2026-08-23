"""The shared task-file readers behave the way the guards built on them assume.

`walk_tasks` and `leaf_tasks` differ only over `block:`, and that difference is exactly what
made the pre-consolidation `_flatten` copies incompatible. A guard that got the wrong one still
passed — it just stopped seeing part of the tree — so the distinction needs its own coverage
rather than relying on the callers to notice.
"""

from __future__ import annotations

import pytest
import yaml

from _helpers import (
    ANSIBLE,
    K8S_ROLES,
    REPO,
    command_of,
    leaf_tasks,
    load_tasks,
    task_named,
    walk_tasks,
)

BLOCKED = yaml.safe_load(
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
