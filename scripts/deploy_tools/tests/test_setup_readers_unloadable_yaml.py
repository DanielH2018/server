"""The setup-role readers answer every unloadable YAML as they answer a syntax error (#7297).

Each case compares a reader's answer on an unloadable text with its answer on a syntax error,
so the syntax-error behaviour is the oracle and no expected value is copied into this file. A
`block:` whose body is not a list does load, so those cases state their answer instead (#7305).

Run: uv run pytest scripts/deploy_tools/tests/test_setup_readers_unloadable_yaml.py
"""

import datetime
from pathlib import Path

import pytest

from deploy_tools.setup_lib import role_hosts
from deploy_tools.setup_lib import setup_gates
from deploy_tools.setup_lib import setup_role_chains as chains

SYNTAX_ERROR = "- [unclosed\n"
IMPOSSIBLE_DATE = "- d: 2026-13-45\n"
BAD_BOOL = "- b: !!bool x\n"
RECURSIVE_ALIAS = "- &x {block: [*x]}\n"
LOADABLE = "- name: Say something\n  ansible.builtin.debug: {}\n"


def _role(root: Path, tasks: str = LOADABLE) -> Path:
    (root / "tasks").mkdir(parents=True)
    (root / "tasks" / "main.yml").write_text(tasks)
    return root


def _task_chains(root: Path, text: str):
    return chains.task_chains(_role(root, text), lambda _task, _file: True)


def _imported_file(root: Path, text: str):
    role = _role(root, "- ansible.builtin.import_tasks: sub.yml\n" + LOADABLE)
    (role / "tasks" / "sub.yml").write_text(text)
    return chains.task_chains(role, lambda _task, _file: True)


def _answer(reader, root: Path, text: str) -> object:
    """What `reader` returns for `text`, or the type of what it raises, for an equality check."""
    try:
        return reader(root, text)
    except Exception as exc:
        return f"raised {type(exc).__name__}"


# One row per `safe_load` call in `setup_role_chains`.
_PARSE_SITES = (_task_chains,)

# The readers that walk a parsed task list.
_RECURSIVE_WALKS = (_task_chains, _imported_file)


def _ids(value) -> str:
    return value.__name__.strip("_") if callable(value) else ""


@pytest.mark.parametrize("reader", _PARSE_SITES, ids=_ids)
def test_an_impossible_date_reads_as_a_syntax_error(tmp_path, reader):
    assert _answer(reader, tmp_path / "date", IMPOSSIBLE_DATE) == _answer(
        reader, tmp_path / "syntax", SYNTAX_ERROR
    )


def test_a_bad_bool_tag_reads_as_a_syntax_error(tmp_path):
    assert _answer(_task_chains, tmp_path / "bool", BAD_BOOL) == _answer(
        _task_chains, tmp_path / "syntax", SYNTAX_ERROR
    )


@pytest.mark.parametrize("reader", _RECURSIVE_WALKS, ids=_ids)
def test_a_recursive_alias_reads_as_a_syntax_error(tmp_path, reader):
    assert _answer(reader, tmp_path / "alias", RECURSIVE_ALIAS) == _answer(
        reader, tmp_path / "syntax", SYNTAX_ERROR
    )


def _block(body: str) -> str:
    return f"- block:{body}\n  when: block_gate\n"


NON_LIST_BODIES = pytest.mark.parametrize(
    "body", ["", " 5"], ids=["empty_block", "number_block"]
)


@NON_LIST_BODIES
def test_task_chains_reads_a_non_list_block_as_holding_no_tasks(tmp_path, body):
    leaf = "- name: Leaf\n  ansible.builtin.debug: {}\n  when: leaf_gate\n"
    role = _role(tmp_path, _block(body) + leaf)
    assert chains.task_chains(role, lambda _task, _file: True) == [("leaf_gate",)]


@NON_LIST_BODIES
def test_role_hosts_keeps_the_role_reach_on_a_role_of_one_non_list_block(
    tmp_path, body
):
    args = _gate_tree(tmp_path / "tree", _block(body))
    every_host = frozenset(setup_gates.HOSTS)
    assert role_hosts.setup_role_hosts("demo", *args) == every_host


def _playbook_reach(root: Path, text: str):
    root.mkdir()
    playbook = root / "initial_setup.yml"
    playbook.write_text(text)
    return role_hosts.setup_role_hosts("common", playbook, roles_dir=root)


def test_role_hosts_reads_a_playbook_date_as_a_syntax_error(tmp_path):
    play = "- hosts: all\n  roles: [common]\n  vars: {d: 2026-13-45}\n"
    assert _answer(_playbook_reach, tmp_path / "date", play) == _answer(
        _playbook_reach, tmp_path / "syntax", SYNTAX_ERROR
    )


def test_a_date_in_an_imported_task_file_keeps_its_chain(tmp_path):
    """A date loads, so refusing the file would drop its chain and narrow the hosts."""
    dated = "- name: Say something\n  src: shipped.j2\n  vars: {d: 2026-01-01}\n"
    role = _role(tmp_path, "- ansible.builtin.import_tasks: sub.yml\n")
    (role / "tasks" / "sub.yml").write_text(dated)
    assert chains.task_chains(role, lambda _task, _file: True) == [()]


def _gate_tree(root: Path, tasks: str, all_vars: str = "ups_host: daniel-server\n"):
    """A playbook running `demo`, and the arguments the host readers take after it."""
    root.mkdir()
    (root / "initial_setup.yml").write_text("- hosts: x\n  roles: [demo]\n")
    (root / "all.yml").write_text(all_vars)
    (root / "host_vars").mkdir()
    (root / "roles").mkdir()
    _role(root / "roles" / "demo", tasks)
    return (
        root / "initial_setup.yml",
        root / "all.yml",
        root / "host_vars",
        root / "roles",
    )


def test_a_date_in_a_when_chain_reads_as_every_host(tmp_path):
    _, all_vars, host_vars, roles = _gate_tree(tmp_path / "tree", LOADABLE)
    chain = (datetime.date(2026, 1, 1),)
    hosts = frozenset(setup_gates.HOSTS)
    assert (
        role_hosts._hosts_passing([chain], hosts, all_vars, host_vars, roles / "demo")
        == hosts
    )


@pytest.mark.parametrize("vars_text", [IMPOSSIBLE_DATE, BAD_BOOL, SYNTAX_ERROR])
def test_a_vars_file_that_does_not_load_reads_as_every_host(tmp_path, vars_text):
    _, all_vars, host_vars, _ = _gate_tree(tmp_path / "tree", LOADABLE, vars_text)
    assert setup_gates._eval_when("no_such_var == 1", "daniel-box", all_vars, host_vars)


@pytest.mark.parametrize("text", [IMPOSSIBLE_DATE, BAD_BOOL])
def test_the_end_role_guard_read_refuses_as_a_syntax_error_does(tmp_path, text):
    def guard(root: Path, tasks: str):
        return setup_gates.role_end_guard(_role(root, tasks))

    assert _answer(guard, tmp_path / "bad", text) == _answer(
        guard, tmp_path / "syntax", SYNTAX_ERROR
    )
