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

Run: uv run pytest ansible/tests/test_has_flag_roles_have_both_directions.py
"""

import re

import pytest
from _helpers import ANSIBLE


SETUP_ROLES = ANSIBLE / "roles" / "setup"

# `when: has_thing` / `when: not has_thing`, as a whole-word match so has_docker does not
# also satisfy a hypothetical has_docker_extras.
_POSITIVE = re.compile(r"^\s*when:\s*(has_\w+)\s*$", re.M)
_NEGATIVE = re.compile(r"^\s*when:\s*not\s+(has_\w+)\s*$", re.M)


def _dispatcher_roles():
    """Roles whose tasks/main.yml includes something on a bare has_* flag."""
    found = []
    for role_dir in sorted(SETUP_ROLES.iterdir()):
        main = role_dir / "tasks" / "main.yml"
        if not main.is_file():
            continue
        flags = set(_POSITIVE.findall(main.read_text()))
        for flag in sorted(flags):
            found.append((role_dir.name, flag))
    return found


def test_some_dispatcher_roles_exist():
    """Guard against the discovery regex silently matching nothing."""
    assert _dispatcher_roles(), (
        "found no setup role dispatching on a has_* flag -- check the matcher"
    )


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
