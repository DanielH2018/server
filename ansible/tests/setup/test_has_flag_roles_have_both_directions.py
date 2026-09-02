#!/usr/bin/env python3
"""A role that dispatches on a has_* flag must handle both values of it.

WHY THIS IS A TEST AND NOT A COMMENT. docker_install was install-only, gated in
initial_setup.yml by `when: has_docker`. Flipping a host to has_docker: false therefore
skipped the role entirely, and nothing declarative reaped what Docker left behind -- so
`has_docker: false` in host_vars described an intention that no code converged to.

The bill arrived twice. daniel-server's 2026-08-14 uninstall was done imperatively and
missed a still-enabled docker-compose unit and two crons. Then docker-ce was reinstalled
on 2026-08-19 and ran for eight days, because the flag that said it should not be there
drove nothing on the false branch.

The fix was to make the role dispatch internally on the flag, with an install half and a
teardown half. This asserts the shape stays that way: a tasks/main.yml that includes one
file `when: <flag>` must also include one `when: not <flag>`. Roles gated in a playbook
rather than internally are not matched -- the pattern here is specifically the dispatcher.

Run: uv run pytest ansible/tests/setup/test_has_flag_roles_have_both_directions.py
"""

import re

import pytest
import yaml
from _helpers import ANSIBLE


SETUP_ROLES = ANSIBLE / "roles" / "setup"

# The DISPATCH shape: a task that pulls in a whole file on a bare has_* flag. Matching every
# bare `when: has_*` instead was too loose -- a plain task gated on a capability flag is not a
# dispatcher and has no teardown half to write. That over-match first bit on 2026-09-02, when
# sops_setup gated one task on `has_repo_checkout` and was told to write a teardown for a
# collections install. Read from the parsed YAML rather than by regex, so the include and its
# `when` are known to belong to the same task.
_INCLUDE_KEYS = ("ansible.builtin.include_tasks", "ansible.builtin.import_tasks")
_NEGATIVE = re.compile(r"^\s*when:\s*not\s+(has_\w+)\s*$", re.M)

# The census must keep finding these. Named rather than counted, so narrowing the matcher fails
# with the member it dropped instead of quietly protecting nothing -- which is the exact way
# this guard could go green while checking less than it did.
KNOWN_DISPATCHERS = frozenset(
    {("docker_install", "has_docker"), ("hypervisor", "has_hypervisor")}
)


def _dispatcher_roles():
    """Roles whose tasks/main.yml pulls in a file on a bare has_* flag."""
    found = []
    for role_dir in sorted(SETUP_ROLES.iterdir()):
        main = role_dir / "tasks" / "main.yml"
        if not main.is_file():
            continue
        try:
            tasks = yaml.safe_load(main.read_text()) or []
        except yaml.YAMLError:
            continue
        if not isinstance(tasks, list):
            continue
        flags = set()
        for task in tasks:
            if not isinstance(task, dict):
                continue
            if not any(key in task for key in _INCLUDE_KEYS):
                continue
            when = task.get("when")
            if isinstance(when, str) and re.fullmatch(r"has_\w+", when.strip()):
                flags.add(when.strip())
        found += [(role_dir.name, flag) for flag in sorted(flags)]
    return found


def test_some_dispatcher_roles_exist():
    """Guard against the discovery matcher silently finding nothing."""
    assert _dispatcher_roles(), (
        "found no setup role dispatching on a has_* flag -- check the matcher"
    )


def test_the_known_dispatchers_are_still_found():
    """Non-vacuity by name. `test_some_dispatcher_roles_exist` only proves the set is non-empty,
    so it would stay green if the matcher dropped one of the two roles this guard exists for."""
    missing = KNOWN_DISPATCHERS - set(_dispatcher_roles())
    assert not missing, f"no longer recognised as dispatchers: {sorted(missing)}"


def test_a_plain_gated_task_is_not_a_dispatcher():
    """The rejecting half. sops_setup gates one task on has_repo_checkout and includes no file,
    so it must not be asked for a teardown branch."""
    assert ("sops_setup", "has_repo_checkout") not in _dispatcher_roles()


@pytest.mark.parametrize("role,flag", _dispatcher_roles())
def test_dispatcher_role_handles_the_false_branch(role, flag):
    main = SETUP_ROLES / role / "tasks" / "main.yml"
    negatives = set(_NEGATIVE.findall(main.read_text()))
    assert flag in negatives, (
        f"{role}/tasks/main.yml dispatches on `{flag}` but has no `when: not {flag}` "
        f"branch. A host that turns {flag} off then gets nothing at all, so the flag "
        f"describes an intention rather than a state -- see this module's docstring for "
        f"the eight days that cost."
    )
