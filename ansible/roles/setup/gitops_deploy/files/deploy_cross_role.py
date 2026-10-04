# ansible/roles/setup/gitops_deploy/files/deploy_cross_role.py
"""The setup files other roles ship or import by path, and the roles a change to one reaches.

`deploy_changes.services_from_changed_paths` and `setup_roles_for` read these tables to
re-apply, or defer-and-alert, every role holding a copy of a changed file. They are static
because every caller passes paths alone, and
`ansible/tests/setup/test_setup_cross_role_files.py` holds each one to the tree.
"""

from __future__ import annotations

# A file one setup role installs from another's `files/`, or imports from another's `tasks/`,
# mapped to those roles, so a change to it re-applies them beside the owner (#3306, #3317).
# A template a shared task file renders inherits that task file's importers (#3319): the
# kuma-check pair reaches a host only through `kuma_check_timer.yml`.
# `ansible/tests/setup/test_setup_cross_role_files.py` holds it to the tree.
_COMMON = "ansible/roles/setup/common"
_HOST_LIB = frozenset(
    {"fake_remux", "gitops_deploy", "k3s", "renovate_agent", "renovate_notify"}
)
_STAMPED = _HOST_LIB | {"claude_code", "deploy_ui", "initial_setup"}
_KUMA_CHECK = frozenset({"gitops_deploy", "initial_setup", "k3s", "render_records"})
SETUP_FILES_SHIPPED_BY_OTHER_ROLES: dict[str, frozenset[str]] = {
    f"{_COMMON}/tasks/agent_user.yml": frozenset({"claude_code", "renovate_agent"}),
    f"{_COMMON}/files/host_lib.py": _HOST_LIB,
    f"{_COMMON}/tasks/install_host_lib.yml": _HOST_LIB,
    f"{_COMMON}/tasks/kuma_check_timer.yml": _KUMA_CHECK,
    f"{_COMMON}/templates/kuma-check.service.j2": _KUMA_CHECK,
    f"{_COMMON}/templates/kuma-check.timer.j2": _KUMA_CHECK,
    f"{_COMMON}/tasks/release_bin.yml": frozenset({"k3s"}),
    f"{_COMMON}/tasks/stamp_deployed.yml": _STAMPED,
    f"{_COMMON}/tasks/stamp_render.yml": _STAMPED | {"nut_host"},
    "ansible/roles/setup/gitops_deploy/files/gitops_markers.py": frozenset(
        {"deploy_ui", "renovate_agent"}
    ),
    "ansible/roles/setup/gitops_deploy/files/gitops_ledger.py": frozenset(
        {"deploy_ui", "renovate_agent"}
    ),
}


# DECIDED: `common/templates/resolv.conf.j2` stays out of the table above and records `common`
# (#3319). `k3s` and `optimize_pi` render it directly, but `optimize_pi` applies only on
# daniel-pi (`when: inventory_hostname == optimize_pi_host`), so the tick's `initial_setup.yml
# --tags optimize_pi` on daniel-box would skip it and still record the apply. Recording
# `common` prints the two-host remediation in `deploy_remediation._setup_commands` instead.
SETUP_FILES_ROUTED_TO_OWNER = frozenset({f"{_COMMON}/templates/resolv.conf.j2"})

# The k8s roles that import a setup file by path, so a change to it defer-and-alerts each one
# (#3320). The deployer never applies a k8s role for a change that is not an image-pin bump, so
# naming them in `cs.k8s` is what keeps their copy of `host_lib.py` from going stale unseen.
# `ansible/tests/setup/test_setup_cross_role_files.py` holds it to the tree.
_K8S_HOST_LIB = frozenset({"configarr", "janitorr"})
K8S_ROLES_IMPORTING_SETUP_FILES: dict[str, frozenset[str]] = {
    f"{_COMMON}/files/host_lib.py": _K8S_HOST_LIB,
    f"{_COMMON}/tasks/install_host_lib.yml": _K8S_HOST_LIB,
    f"{_COMMON}/tasks/stamp_deployed.yml": _K8S_HOST_LIB,
}


# This module, by its repo path, and the names of the tables above.
CROSS_ROLE_FILE = "ansible/roles/setup/gitops_deploy/files/deploy_cross_role.py"
TABLE_NAMES = (
    "K8S_ROLES_IMPORTING_SETUP_FILES",
    "SETUP_FILES_ROUTED_TO_OWNER",
    "SETUP_FILES_SHIPPED_BY_OTHER_ROLES",
)


def tables_in(source: str) -> dict[str, object]:
    """The tables a copy of this module defines, by name (#3512).

    The tick classifies with the installed `/opt/gitops-deploy` copy, which predates a range
    that edits this file. A PR adding a `common/tasks` file and its table entry together then
    found no shippers and recorded `common` in `manual_plane`, so `deploy_phases.plan_tick`
    reads origin's copy through this and passes it to `use_tables`.

    Raises:
        Exception: whatever `source` raises when run, or KeyError when it lacks a table.
    """
    namespace: dict[str, object] = {}
    # DECIDED: exec origin's copy rather than parse it. The tables are built with set unions
    # and f-strings, which `ast.literal_eval` refuses. Origin has passed the CI gate, and the
    # same tick's `gitops-deploy-code` apply installs and imports this exact file.
    exec(compile(source, CROSS_ROLE_FILE, "exec"), namespace)
    missing = [name for name in TABLE_NAMES if name not in namespace]
    if missing:
        raise KeyError(f"{CROSS_ROLE_FILE} defines no {', '.join(missing)}")
    return {name: namespace[name] for name in TABLE_NAMES}


def current_tables() -> dict[str, object]:
    """The tables this module holds, by name, in the shape `use_tables` takes."""
    return {name: globals()[name] for name in TABLE_NAMES}


def use_tables(tables: dict[str, object]) -> None:
    """Rebind this module's tables for the rest of the process.

    A reader that looks a table up as `deploy_cross_role.<NAME>` at call time sees the new
    one. A name bound by `from deploy_cross_role import` keeps the old.
    """
    globals().update({name: tables[name] for name in TABLE_NAMES})
