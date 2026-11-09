"""Every variable the revert runbook's task files read still resolves.

The runbook's task files outlive the k8s play and the manifests role, which set most of what
they read at the time they were written. Each name resolves from the two roles' own defaults,
`group_vars/all.yml`, a `register`, `set_fact` or loop variable in those files, one of the three
`-e` inputs the runbook passes, or `MAGIC`. A gathered fact resolves only when the play gathers,
so the playbook's gather is checked too.

Run: uv run pytest ansible/tests/longhorn/test_volume_revert_runbook_resolves.py
"""

import shutil
from pathlib import Path

import yaml
from jinja2 import nodes

from _helpers import ANSIBLE, walk_tasks
from _jinja_env import jinja_env
from _role_census import role_task_files
from lib import yaml_fast
from lib.repo_paths import ALL_VARS, K8S_ROLES

ROLES = (K8S_ROLES / "volume-revert", K8S_ROLES / "longhorn-api")
PLAYBOOK = ANSIBLE / "volume-revert.yml"
RUNBOOK_INPUTS = frozenset(
    {"volume_revert_service", "volume_revert_claims", "volume_revert_sha"}
)
# Named one by one, because a prefix would admit a fact the play does not gather.
# `ansible_check_mode` is a magic variable the value of `k8s_no_mutate` reads, and
# `ansible_hostname` is a gathered fact `longhorn-api/tasks/resolve.yml` reads.
MAGIC = frozenset({"ansible_check_mode", "ansible_hostname"})
# Task keys whose value is a bare expression rather than a templated string.
_EXPRESSION_KEYS = frozenset({"when", "until", "failed_when", "changed_when", "that"})
_SET_FACT = frozenset({"set_fact", "ansible.builtin.set_fact"})


def _expression_reads(source: str) -> set[str]:
    """The names a Jinja source loads and does not assign itself."""
    tree = jinja_env().parse(source)
    loaded = {n.name for n in tree.find_all(nodes.Name) if n.ctx == "load"}
    stored = {n.name for n in tree.find_all(nodes.Name) if n.ctx != "load"}
    return loaded - stored


def _value_reads(value: object, bare: bool = False) -> set[str]:
    """The names a task value reads, with `bare` for a key whose string is an expression."""
    if isinstance(value, dict):
        return {
            name
            for key, item in value.items()
            for name in _value_reads(item, bare or key in _EXPRESSION_KEYS)
        }
    if isinstance(value, list):
        return {name for item in value for name in _value_reads(item, bare)}
    if not isinstance(value, str):
        return set()
    if bare:
        return _expression_reads("{{ " + value + " }}")
    return _expression_reads(value)


def _task_files(roles: tuple[Path, ...]) -> list[Path]:
    return [path for role in roles for path in role_task_files(role)]


def _task_sets(tasks: list[dict]) -> set[str]:
    """The names a task file assigns: registers, facts and loop variables."""
    found = {"item"}
    for task in tasks:
        if isinstance(task.get("register"), str):
            found.add(task["register"])
        for module in _SET_FACT & task.keys():
            found |= set(task[module] or {})
        loop_var = (task.get("loop_control") or {}).get("loop_var")
        if loop_var:
            found.add(loop_var)
    return found


def unresolved(
    roles: tuple[Path, ...] = ROLES, group_vars: Path = ALL_VARS
) -> set[str]:
    """The names the task files read, directly or through a value, that nothing supplies."""
    defined: dict[str, object] = dict(yaml_fast.safe_load(group_vars.read_text()))
    for role in roles:
        defined |= yaml_fast.safe_load((role / "defaults/main.yml").read_text()) or {}
    assigned: set[str] = set()
    pending: set[str] = set()
    for path in _task_files(roles):
        tasks = list(walk_tasks(yaml_fast.safe_load(path.read_text()) or []))
        assigned |= _task_sets(tasks)
        pending |= _value_reads(tasks)
    missing: set[str] = set()
    seen: set[str] = set()
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        if name in defined:
            pending |= _value_reads(defined[name])
        elif name not in assigned | RUNBOOK_INPUTS | MAGIC:
            missing.add(name)
    return missing


def play_gathers(playbook: Path = PLAYBOOK) -> bool:
    """Whether every play gathers facts itself or through the shared preamble's setup task."""
    plays = yaml_fast.safe_load(playbook.read_text())
    return bool(plays) and all(_gathers(play) for play in plays)


def _gathers(play: dict) -> bool:
    if play.get("gather_facts", True) is not False:
        return True
    if (play.get("vars") or {}).get("load_secrets_gather_facts") is False:
        return False
    tasks = walk_tasks(play.get("pre_tasks") or [])
    return any(
        str(task.get("ansible.builtin.import_tasks", "")).endswith("load_secrets.yml")
        or "ansible.builtin.setup" in task
        for task in tasks
    )


def _fixture_roles(tmp_path: Path) -> tuple[Path, ...]:
    copies = []
    for role in ROLES:
        copy = tmp_path / role.name
        shutil.copytree(role, copy, ignore=shutil.ignore_patterns("tests"))
        copies.append(copy)
    return tuple(copies)


def test_every_name_the_runbook_reads_resolves_on_the_tree():
    assert unresolved() == set()


def test_the_census_reads_the_names_the_runbook_depends_on():
    """Non-vacuity: a read that stopped finding these would resolve everything."""
    reads = {
        name
        for path in _task_files(ROLES)
        for name in _value_reads(yaml_fast.safe_load(path.read_text()))
    }
    assert {"k8s_no_mutate", "k8s_namespace", "ansible_hostname"} <= reads
    assert RUNBOOK_INPUTS <= reads


def test_a_group_vars_copy_without_k8s_no_mutate_is_flagged(tmp_path):
    group_vars = tmp_path / "all.yml"
    defined = yaml_fast.safe_load(ALL_VARS.read_text())
    del defined["k8s_no_mutate"]
    group_vars.write_text(yaml.safe_dump(defined))
    assert unresolved(group_vars=group_vars) == {"k8s_no_mutate"}


def test_a_value_a_kept_group_var_reads_is_followed(tmp_path):
    """`k8s_no_mutate` reads `k8s_dry_run`, which no task file names."""
    group_vars = tmp_path / "all.yml"
    defined = yaml_fast.safe_load(ALL_VARS.read_text())
    del defined["k8s_dry_run"]
    group_vars.write_text(yaml.safe_dump(defined))
    assert unresolved(group_vars=group_vars) == {"k8s_dry_run"}


def test_a_task_reading_another_gathered_fact_is_flagged(tmp_path):
    roles = _fixture_roles(tmp_path)
    (roles[1] / "tasks" / "extra.yml").write_text(
        "- name: Read a fact the allow-set does not name\n"
        "  ansible.builtin.debug:\n"
        '    msg: "{{ ansible_fqdn }}"\n'
    )
    assert unresolved(roles) == {"ansible_fqdn"}


def test_the_playbook_gathers_through_the_preamble():
    assert play_gathers()


def test_a_playbook_that_gathers_nothing_is_flagged(tmp_path):
    playbook = tmp_path / "volume-revert.yml"
    playbook.write_text(
        "- name: Revert\n  hosts: all\n  gather_facts: false\n  tasks: []\n"
    )
    assert not play_gathers(playbook)


def test_a_playbook_that_opts_out_of_the_preambles_gather_is_flagged(tmp_path):
    playbook = tmp_path / "volume-revert.yml"
    playbook.write_text(
        "- name: Revert\n  hosts: all\n  gather_facts: false\n"
        "  vars:\n    load_secrets_gather_facts: false\n"
        "  pre_tasks:\n    - ansible.builtin.import_tasks: pre_tasks/load_secrets.yml\n"
    )
    assert not play_gathers(playbook)
