"""The setup-role readers answer every unloadable YAML as they answer a syntax error (#7297).

Each case compares a reader's answer on an unloadable text with its answer on a syntax error,
so the syntax-error behaviour is the oracle and no expected value is copied into this file.

Run: uv run pytest scripts/deploy_tools/tests/test_setup_readers_unloadable_yaml.py
"""

import datetime
from pathlib import Path

import pytest

from deploy_tools.land_lib import land_reach
from deploy_tools.setup_lib import setup_gates
from deploy_tools.setup_lib import setup_role_chains as chains

SYNTAX_ERROR = "- [unclosed\n"
IMPOSSIBLE_DATE = "- d: 2026-13-45\n"
BAD_BOOL = "- b: !!bool x\n"
RECURSIVE_ALIAS = "- &x {block: [*x]}\n"
LOADABLE = "- name: Say something\n  ansible.builtin.debug: {}\n"
HANDLER = "- name: Restart it\n  ansible.builtin.debug: {}\n"


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


def _task_gates_naming(root: Path, text: str):
    return chains.task_gates_naming(_role(root, text), "shipped.j2")


def _var_consumers(root: Path, text: str):
    role = _role(root)
    (role / "defaults").mkdir()
    (role / "defaults" / "main.yml").write_text(text)
    return chains.var_consumer_chains(role, role / "defaults" / "main.yml")


def _handler_notifiers(root: Path, text: str):
    role = _role(root)
    (role / "handlers").mkdir()
    (role / "handlers" / "main.yml").write_text(text)
    return chains.handler_notifier_chains(role, role / "handlers" / "main.yml")


def _changed_old_side(_root: Path, text: str):
    return chains.changed_task_texts(text, LOADABLE)


def _changed_new_side(_root: Path, text: str):
    return chains.changed_task_texts(LOADABLE, text)


def _removed_old_side(root: Path, text: str):
    return chains._removed_task_chains(_role(root), "main.yml", text, LOADABLE)


def _removed_new_side(root: Path, text: str):
    return chains._removed_task_chains(_role(root), "main.yml", LOADABLE, text)


def _import_chains(root: Path, text: str):
    return chains._import_chains(_role(root, text), "other.yml", "main.yml")


def _answer(reader, root: Path, text: str) -> object:
    """What `reader` returns for `text`, or the type of what it raises, for an equality check."""
    try:
        return reader(root, text)
    except Exception as exc:
        return f"raised {type(exc).__name__}"


# One row per `safe_load` call in `setup_role_chains`.
_PARSE_SITES = (
    _task_chains,
    _var_consumers,
    _handler_notifiers,
    _changed_old_side,
    _changed_new_side,
    _removed_old_side,
    _removed_new_side,
)

# The readers that walk a parsed task list. Handlers and vars files parse to a value no walk
# descends, so a recursive alias there already reads as a syntax error does.
_RECURSIVE_WALKS = (
    _task_chains,
    _imported_file,
    _task_gates_naming,
    _changed_old_side,
    _changed_new_side,
    _removed_old_side,
    _removed_new_side,
    _import_chains,
)


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


def _playbook_reach(root: Path, text: str):
    root.mkdir()
    playbook = root / "initial_setup.yml"
    playbook.write_text(text)
    return land_reach.setup_role_hosts("common", playbook, roles_dir=root)


def test_land_reach_reads_a_playbook_date_as_a_syntax_error(tmp_path):
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
    assert chains.task_gates_naming(role, "shipped.j2") == [()]
    assert chains.changed_task_texts(LOADABLE, dated)


def test_a_recursive_alias_below_an_import_keeps_the_var_consumers_wide(tmp_path):
    """Refused mid-walk, `keep` would hold a text whose chain never came back, and `zip` raises."""
    role = _role(tmp_path, "- ansible.builtin.import_tasks: sub.yml\n")
    (role / "tasks" / "sub.yml").write_text(
        "- name: Reads my_var\n  ansible.builtin.debug: {msg: my_var}\n"
        + RECURSIVE_ALIAS
    )
    (role / "defaults").mkdir()
    (role / "defaults" / "main.yml").write_text("my_var: 1\n")
    assert chains.var_consumer_chains(role, role / "defaults" / "main.yml") is None


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


def test_a_shipped_file_read_keeps_the_gate_on_a_task_holding_a_date(tmp_path):
    tasks = (
        "- name: Ships it\n  src: shipped.j2\n  vars: {d: 2026-01-01}\n"
        "  when: inventory_hostname == ups_host\n"
        "- name: Something else\n  ansible.builtin.debug: {}\n"
    )
    args = _gate_tree(tmp_path / "tree", tasks)
    path = "ansible/roles/setup/common/files/shipped.j2"
    assert land_reach.setup_shipped_file_hosts("demo", path, *args) == {"daniel-server"}


def test_a_date_in_a_when_chain_reads_as_every_host(tmp_path):
    _, all_vars, host_vars, roles = _gate_tree(tmp_path / "tree", LOADABLE)
    chain = (datetime.date(2026, 1, 1),)
    hosts = frozenset(land_reach._HOSTS)
    assert (
        land_reach._hosts_passing([chain], hosts, all_vars, host_vars, roles / "demo")
        == hosts
    )


@pytest.mark.parametrize("vars_text", [IMPOSSIBLE_DATE, BAD_BOOL, SYNTAX_ERROR])
def test_a_vars_file_that_does_not_load_reads_as_every_host(tmp_path, vars_text):
    _, all_vars, host_vars, _ = _gate_tree(tmp_path / "tree", LOADABLE, vars_text)
    assert land_reach._eval_when("no_such_var == 1", "daniel-box", all_vars, host_vars)


@pytest.mark.parametrize("text", [IMPOSSIBLE_DATE, BAD_BOOL])
def test_the_end_role_guard_read_refuses_as_a_syntax_error_does(tmp_path, text):
    def guard(root: Path, tasks: str):
        return setup_gates.role_end_guard(_role(root, tasks))

    assert _answer(guard, tmp_path / "bad", text) == _answer(
        guard, tmp_path / "syntax", SYNTAX_ERROR
    )
