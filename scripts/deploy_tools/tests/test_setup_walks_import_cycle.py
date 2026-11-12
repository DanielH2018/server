"""The setup-role walks read an import cycle as an unreadable file (#7301).

A cycle below an import is compared with a syntax error in the same file, so the syntax-error
behaviour is the oracle. Each cycle's file holds nothing but its imports, so "the file is
unreadable" and "the repeated visit is unreadable" give the same answer.

Run: uv run pytest scripts/deploy_tools/tests/test_setup_walks_import_cycle.py
"""

from pathlib import Path

import pytest

from deploy_tools.setup_lib import setup_role_chains as chains

SYNTAX_ERROR = "- [unclosed\n"
GATED_LEAF = "- {name: Leaf, ansible.builtin.debug: {}, when: leaf_gate}\n"
OTHER_KEPT = "- name: Kept\n  ansible.builtin.debug: {}\n"
MAIN = (
    "- ansible.builtin.import_tasks: sub.yml\n"
    "  when: sub_gate\n"
    "- name: Reads my_var and ships shipped.j2\n"
    "  ansible.builtin.debug: {msg: my_var shipped.j2}\n"
    "  notify: Restart it\n"
    "  when: leaf_gate\n"
    "- ansible.builtin.import_tasks: other.yml\n"
    "  when: other_gate\n"
)


def _imports(name: str) -> str:
    return f"- ansible.builtin.import_tasks: {name}\n"


UNREADABLE_SUB = {"sub.yml": SYNTAX_ERROR}
CYCLES = {
    "self": {"sub.yml": _imports("sub.yml")},
    "mutual": {"sub.yml": _imports("sub2.yml"), "sub2.yml": _imports("sub.yml")},
}


def _role(root: Path, task_files: dict[str, str]) -> Path:
    (root / "tasks").mkdir(parents=True)
    for name, text in task_files.items():
        (root / "tasks" / name).write_text(text)
    return root


def _full_role(root: Path, sub_files: dict[str, str]) -> Path:
    return _role(root, {"main.yml": MAIN, "other.yml": OTHER_KEPT, **sub_files})


def _answer(reader, *args) -> object:
    """What `reader` returns, or the type of what it raises, for an equality check."""
    try:
        return reader(*args)
    except Exception as exc:
        return f"raised {type(exc).__name__}"


def _keep_all(_task, _file) -> bool:
    return True


def _task_chains(role: Path):
    return chains.task_chains(role, _keep_all)


@pytest.mark.parametrize(
    "task_files",
    [
        {"main.yml": GATED_LEAF + _imports("main.yml")},
        {"main.yml": GATED_LEAF + _imports("sub.yml"), "sub.yml": _imports("main.yml")},
    ],
    ids=["imports_itself", "imports_itself_through_another_file"],
)
def test_task_chains_returns_on_an_entry_file_in_a_cycle(tmp_path, task_files):
    """None is the unreadable-file answer; otherwise the leaf appears once, on its own gate."""
    answer = _answer(chains.task_chains, _role(tmp_path, task_files), _keep_all)
    assert answer in (None, [("leaf_gate",)])


@pytest.mark.parametrize("cycle", CYCLES.values(), ids=CYCLES.keys())
def test_a_cycle_below_an_import_reads_as_an_unreadable_file(tmp_path, cycle):
    expected = _answer(_task_chains, _full_role(tmp_path / "syntax", UNREADABLE_SUB))
    assert expected
    assert _answer(_task_chains, _full_role(tmp_path / "cycle", cycle)) == expected


def test_a_file_imported_twice_beside_a_cycle_keeps_both_chains(tmp_path):
    """Two imports of one file are not a cycle, so a record of walked files must not merge them."""
    main = (
        "- ansible.builtin.import_tasks: shared.yml\n  when: first_gate\n"
        "- ansible.builtin.import_tasks: shared.yml\n  when: second_gate\n"
        + _imports("sub.yml")
    )
    role = _role(
        tmp_path,
        {"main.yml": main, "shared.yml": OTHER_KEPT, "sub.yml": _imports("sub.yml")},
    )
    assert _answer(chains.task_chains, role, _keep_all) == [
        ("first_gate",),
        ("second_gate",),
    ]
