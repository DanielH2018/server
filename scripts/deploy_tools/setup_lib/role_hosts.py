"""Which hosts `initial_setup.yml` runs at least one task of a setup role on.

`setup_census.py` and the two-writer censuses in `ansible/tests/setup/` read `setup_role_hosts`
to decide which hosts a role and a host package can both write to. H7.20a deletes it with them.

Run: uv run pytest ansible/tests/setup/test_host_package_two_writers.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # scripts/

from lib import yaml_fast
from lib.repo_paths import ALL_VARS, ANSIBLE, HOST_VARS
from deploy_tools.setup_lib.setup_gates import HOSTS, _eval_when, role_end_hosts
from deploy_tools.setup_lib.setup_role_chains import task_chains, task_text


_INITIAL_SETUP_YML = ANSIBLE / "initial_setup.yml"


def _initial_setup_roles(playbook: Path = _INITIAL_SETUP_YML) -> dict[str, object]:
    """{role name: its `when:` value (a string, a list, a bool, or None)}.

    Read from `playbook`'s own `roles:` list -- the same source Ansible itself resolves
    against, so this can never disagree with what a real run does. `playbook` defaults to
    this repo's `initial_setup.yml`; a test passes a synthetic one so the derivation it pins
    cannot drift when this repo's own gates change.
    """
    play = yaml_fast.parse_yaml(playbook.read_text())[0]
    roles: dict[str, object] = {}
    for entry in play["roles"]:
        name = entry if isinstance(entry, str) else entry["role"]
        when = None if isinstance(entry, str) else entry.get("when")
        roles[name] = when
    return roles


_SETUP_ROLES_DIR = ANSIBLE / "roles" / "setup"


def setup_role_hosts(
    role: str,
    playbook: Path = _INITIAL_SETUP_YML,
    all_vars: Path = ALL_VARS,
    host_vars_dir: Path = HOST_VARS,
    roles_dir: Path = _SETUP_ROLES_DIR,
) -> frozenset[str]:
    """Which of `HOSTS` `initial_setup.yml` runs at least one task of `role` on.

    `initial_setup.yml`'s own `hosts:` is `{{ target | default(lookup('pipe','hostname')) }}`
    -- one host per run -- so a role with NO `when:` gate reaches every host the playbook is
    EVER run on.

    A role whose first task is `meta: end_role` under a `when:` is not run on the hosts where
    that guard passes (`setup_gates.role_end_hosts`), whatever its playbook gate says.

    A role with no playbook gate is read from its own tasks instead of assumed to reach
    every host: a host is reached when at least one leaf task's `when:` chain -- the block and
    static-import gates above it included -- passes there. A tasks tree that cannot be read
    stays wide, the same asymmetry `_eval_when` applies inside one gate.

    Returns an empty set for a role `initial_setup.yml` does not reach at all (it is not in
    that playbook's `roles:` list).
    """
    roles = _initial_setup_roles(playbook)
    if role not in roles:
        return frozenset()
    # The role's leading `meta: end_role` guard ends it on a host before any task runs there.
    hosts = frozenset(HOSTS) - role_end_hosts(
        roles_dir / role, HOSTS, all_vars, host_vars_dir
    )
    when = roles[role]
    if when is None:
        chains = task_chains(roles_dir / role, lambda task, task_file: True)
        if not chains:
            return hosts
        return _hosts_passing(chains, hosts, all_vars, host_vars_dir, roles_dir / role)
    return frozenset(h for h in hosts if _eval_when(when, h, all_vars, host_vars_dir))


def _hosts_passing(
    chains, hosts, all_vars: Path, host_vars_dir: Path, role_dir: Path
) -> frozenset[str]:
    """The hosts of `hosts` on which at least one `when:` chain in `chains` passes whole.

    Each gate is read with `role_dir`'s `defaults/main.yml` under `group_vars`, as Ansible
    reads a task of that role, so a `<role>_packaged_hosts` guard resolves.

    An empty chain -- an ungated leaf -- passes everywhere, so its presence settles every
    host before a single gate is read. The rest are deduplicated first: every leaf under
    one block carries the same `("has_gitops",)`, and `_eval_when` re-parses
    group_vars/all.yml per gate per host, which put a 40-path `initial_setup` note at 4.7s
    against 0.24s before the role-level read existed.
    """
    if () in chains:
        return frozenset(hosts)
    distinct = {task_text(chain, sort_keys=True): chain for chain in chains}.values()
    defaults = role_dir / "defaults" / "main.yml"
    return frozenset(
        h
        for h in hosts
        if any(
            all(_eval_when(g, h, all_vars, host_vars_dir, defaults) for g in chain)
            for chain in distinct
        )
    )
