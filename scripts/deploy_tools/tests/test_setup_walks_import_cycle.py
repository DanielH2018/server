"""The setup-role walks read an import cycle as an unreadable file (#7301).

A cycle below an import is compared with a syntax error in the same file, so the syntax-error
behaviour is the oracle. Each cycle's file holds nothing but its imports, so "the file is
unreadable" and "the repeated visit is unreadable" give the same answer.

Run: uv run pytest scripts/deploy_tools/tests/test_setup_walks_import_cycle.py
"""

from pathlib import Path

import pytest

from deploy_tools.land_lib import land_reach
from deploy_tools.setup_lib import setup_role_chains as chains

SYNTAX_ERROR = "- [unclosed\n"
GATED_LEAF = "- {name: Leaf, ansible.builtin.debug: {}, when: leaf_gate}\n"
OTHER_KEPT = "- name: Kept\n  ansible.builtin.debug: {}\n"
OTHER_REMOVED = "- name: Removed\n  ansible.builtin.debug: {}\n"
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
    role = _role(
        root, {"main.yml": MAIN, "other.yml": OTHER_KEPT + OTHER_REMOVED, **sub_files}
    )
    (role / "defaults").mkdir()
    (role / "defaults" / "main.yml").write_text("my_var: 1\n")
    (role / "handlers").mkdir()
    (role / "handlers" / "main.yml").write_text(
        "- name: Restart it\n  ansible.builtin.debug: {}\n"
    )
    return role


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


def _task_gates_naming(role: Path):
    return chains.task_gates_naming(role, "shipped.j2")


def _var_consumers(role: Path):
    return chains.var_consumer_chains(role, role / "defaults" / "main.yml")


def _handler_notifiers(role: Path):
    return chains.handler_notifier_chains(role, role / "handlers" / "main.yml")


def _import_chains(role: Path):
    return chains._import_chains(role, "other.yml", "main.yml")


def _removed_task_chains(role: Path):
    return chains._removed_task_chains(
        role, "other.yml", OTHER_KEPT + OTHER_REMOVED, OTHER_KEPT
    )


def _ids(value) -> str:
    return value.__name__.strip("_") if callable(value) else ""


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


def test_import_chains_returns_on_an_entry_file_importing_itself(tmp_path):
    role = _role(tmp_path, {"main.yml": _imports("main.yml")})
    assert _answer(chains._import_chains, role, "other.yml", "main.yml") == []


@pytest.mark.parametrize("cycle", CYCLES.values(), ids=CYCLES.keys())
@pytest.mark.parametrize(
    "reader",
    [
        _task_chains,
        _task_gates_naming,
        _var_consumers,
        _handler_notifiers,
        _import_chains,
        _removed_task_chains,
    ],
    ids=_ids,
)
def test_a_cycle_below_an_import_reads_as_an_unreadable_file(tmp_path, reader, cycle):
    expected = _answer(reader, _full_role(tmp_path / "syntax", UNREADABLE_SUB))
    assert expected
    assert _answer(reader, _full_role(tmp_path / "cycle", cycle)) == expected


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


def test_a_target_imported_through_two_chains_beside_a_cycle_keeps_both(tmp_path):
    main = (
        "- ansible.builtin.import_tasks: shared.yml\n  when: first_gate\n"
        "- ansible.builtin.import_tasks: shared.yml\n  when: second_gate\n"
        + _imports("sub.yml")
    )
    role = _role(
        tmp_path,
        {
            "main.yml": main,
            "shared.yml": _imports("other.yml"),
            "sub.yml": _imports("sub.yml"),
        },
    )
    assert _answer(chains._import_chains, role, "other.yml", "main.yml") == [
        ("first_gate",),
        ("second_gate",),
    ]


def _shipped_file_hosts(root: Path, sub_files: dict[str, str]):
    root.mkdir()
    (root / "initial_setup.yml").write_text("- hosts: x\n  roles: [demo]\n")
    (root / "all.yml").write_text("leaf_gate: true\nsub_gate: true\nother_gate: true\n")
    (root / "host_vars").mkdir()
    (root / "roles").mkdir()
    _full_role(root / "roles" / "demo", sub_files)
    return land_reach.setup_shipped_file_hosts(
        "demo",
        "ansible/roles/setup/demo/files/shipped.j2",
        root / "initial_setup.yml",
        root / "all.yml",
        root / "host_vars",
        root / "roles",
    )


def test_land_reach_reads_a_cycle_as_an_unreadable_file(tmp_path):
    expected = _answer(_shipped_file_hosts, tmp_path / "syntax", UNREADABLE_SUB)
    assert expected
    assert _answer(_shipped_file_hosts, tmp_path / "cycle", CYCLES["self"]) == expected
