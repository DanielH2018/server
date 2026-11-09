# ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_cross_role.py
"""The files other roles ship, import or read by path, and the roles a change to one reaches.

`deploy_changes.services_from_changed_paths` and `setup_roles_for` read the setup tables below to
re-apply, or defer-and-alert, every role holding a copy of a changed file, and every setup
role calling a changed filter plugin's filters. They are static because every caller passes
paths alone, and `ansible/tests/setup/test_setup_cross_role_files.py` holds each one to the
tree.

The k8s plane needs no table, because the renderer reads every file a role's render holds.

A module of its own, not part of `deploy_changes`, because the tick and `land.sh` parse
another commit's copy of THIS file by path (`tables_in`, `CROSS_ROLE_FILE`). Nothing else may
share the file that parse reads.
"""

from __future__ import annotations

import ast

# A file one setup role installs from another's `files/`, imports from another's `tasks/`, or
# reads a variable of from another's `defaults/`, mapped to those roles, so a change to it re-applies them beside the owner (#3306, #3317).
# A template a shared task file renders inherits that task file's importers (#3319).
# `ansible/tests/setup/test_setup_cross_role_files.py` holds it to the tree, and
# `test_setup_cross_role_reads.py` holds its `defaults/` rows.
_COMMON = "ansible/roles/setup/common"
_HOST_LIB = frozenset({"gitops_deploy"})
SETUP_FILES_SHIPPED_BY_OTHER_ROLES: dict[str, frozenset[str]] = {
    # `hosts/lib/` since H7.2e: the packages ship it, and the Ansible consumers below still copy
    # it beside their scripts until their own PRs move them. `ci_verdict.py` is the deployer's
    # alone (`gitops_deploy/tasks/code.yml` copies it).
    "hosts/lib/host_lib.py": _HOST_LIB,
    "hosts/lib/ci_verdict.py": frozenset({"gitops_deploy"}),
}


# A setup-tree template that no setup role imports and one role renders by path, mapped to the
# roles that render it. Empty since H7.35: the host package builder renders
# `common/templates/resolv.conf.j2`, through the `pi-resolver` and `k3s-host-dns` components.
SETUP_FILES_ROUTED_TO_OWNER: dict[str, frozenset[str]] = {}

# A setup-tree template that only host packages render, through a component's `role:`, and no
# setup task names. The package applies it, so a change re-applies no setup role and owes no
# `manual_plane` line. `test_setup_cross_role_files.py` holds it equal to the components.
_INITIAL_SETUP = "ansible/roles/setup/initial_setup/templates"
_NUT_HOST = "ansible/roles/setup/nut_host/templates"
SETUP_FILES_RENDERED_ONLY_BY_HOST_PACKAGES: frozenset[str] = frozenset(
    {
        f"{_COMMON}/templates/resolv.conf.j2",
        f"{_INITIAL_SETUP}/98_aide_local.conf.j2",
        f"{_INITIAL_SETUP}/aide-check.sh.j2",
        f"{_INITIAL_SETUP}/fail2ban_homelab.conf.j2",
        f"{_INITIAL_SETUP}/rkhunter-check.sh.j2",
        f"{_INITIAL_SETUP}/rkhunter.conf.local.j2",
        f"{_NUT_HOST}/host-upsmon.conf.j2",
        f"{_NUT_HOST}/ups-secondary-health.sh.j2",
    }
)

# A filter plugin mapped to the setup roles whose rendered state calls one of its filters
# (#3874). `ansible/deploy.yml` runs no setup role, so no deploy-plane apply, narrowed or
# whole, re-renders these. `setup_roles_for` returns them, and the tick
# applies or records each as it would its own change: `gitops_deploy` through
# `initial_setup.yml`, and `k3s` as a `manual_plane` line, since only `k3s-bringup.yml` runs it.
# `ansible/tests/setup/test_setup_cross_role_reads.py` holds it to the tree.
SETUP_ROLES_CALLING_FILTER_PLUGINS: dict[str, frozenset[str]] = {}


# This module, by its repo path, and the names of the tables above.
CROSS_ROLE_FILE = (
    "ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_cross_role.py"
)
TABLE_NAMES = (
    "SETUP_FILES_RENDERED_ONLY_BY_HOST_PACKAGES",
    "SETUP_FILES_ROUTED_TO_OWNER",
    "SETUP_FILES_SHIPPED_BY_OTHER_ROLES",
    "SETUP_ROLES_CALLING_FILTER_PLUGINS",
)


def tables_in(source: str) -> dict[str, object]:
    """The tables a copy of this module defines, by name, read without running it (#3512).

    The tick classifies with the installed `/opt/gitops-deploy` copy, which predates a range
    that edits this file. A PR adding a `common/tasks` file and its table entry together then
    found no shippers and recorded `common` in `manual_plane`, so `deploy_phases.plan_tick`
    reads origin's copy through this and passes it to `use_tables`. `land.sh` does the same
    with the merge commit's copy (`classify.adopt_cross_role_tables`).

    Raises:
        SyntaxError: when `source` does not parse.
        KeyError: when a table is missing, or its value is an expression `_value` refuses.
    """
    # DECIDED: parse the copy rather than exec it. `land.sh` reads the merge commit's copy
    # BEFORE it waits on master CI, so running it would execute code no gate has passed. The
    # tables use set unions, `frozenset()` and f-strings, which `ast.literal_eval` refuses,
    # so `_value` evaluates exactly those node types and none that can call out.
    namespace: dict[str, object] = {}
    for node in ast.parse(source, CROSS_ROLE_FILE).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target, value = node.target, node.value
        else:
            continue
        if not isinstance(target, ast.Name):
            continue
        try:
            namespace[target.id] = _value(value, namespace)
        except ValueError, TypeError:
            # An unreadable value unbinds the name, so a table built on it goes missing
            # rather than keeping an earlier binding.
            namespace.pop(target.id, None)
    missing = [name for name in TABLE_NAMES if name not in namespace]
    if missing:
        raise KeyError(f"{CROSS_ROLE_FILE} defines no {', '.join(missing)}")
    return {name: namespace[name] for name in TABLE_NAMES}


_SET_CALLS = {"frozenset": frozenset, "set": set}


def _value(node: ast.expr, names: dict[str, object]) -> object:
    """One table expression's value, built from literals and the names bound above it.

    Admits constants, names, f-strings, tuples and lists (both as tuples), sets, dicts with `**` unpacking, `|`,
    and a bare `frozenset(...)` or `set(...)` call. Anything else raises ValueError.
    """
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id not in names:
            raise ValueError(f"unbound name {node.id}")
        return names[node.id]
    if isinstance(node, ast.JoinedStr):
        return "".join(_fstring_part(part, names) for part in node.values)
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        items = [_value(e, names) for e in node.elts]
        return set(items) if isinstance(node, ast.Set) else tuple(items)
    if isinstance(node, ast.Dict):
        out: dict = {}
        for key, val in zip(node.keys, node.values, strict=True):
            if key is None:
                out.update(_collection(_value(val, names), dict))
            else:
                out[_value(key, names)] = _value(val, names)
        return out
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        left = _collection(_value(node.left, names), (set, frozenset, dict))
        return left | _value(node.right, names)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in _SET_CALLS
        and len(node.args) <= 1
        and not node.keywords
    ):
        arg = _value(node.args[0], names) if node.args else ()
        arg = _collection(arg, (tuple, list, set, frozenset))
        return _SET_CALLS[node.func.id](arg)
    raise ValueError(f"unsupported expression {type(node).__name__}")


def _fstring_part(node: ast.expr, names: dict[str, object]) -> str:
    """One piece of an f-string: literal text, or a plain `{name}` holding a string."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.FormattedValue) and node.conversion == -1:
        if node.format_spec is None:
            return _collection(_value(node.value, names), str)
    raise ValueError("an f-string part other than text or a plain string name")


def _collection(value, kinds):
    """`value` when it is one of `kinds`, else ValueError."""
    if not isinstance(value, kinds):
        raise ValueError(f"expected {kinds}, got {type(value).__name__}")
    return value


def current_tables() -> dict[str, object]:
    """The tables this module holds, by name, in the shape `use_tables` takes."""
    return {name: globals()[name] for name in TABLE_NAMES}


def setup_roles_outside_the_tree(path: str) -> set[str]:
    """The setup roles a file outside `roles/setup/` re-applies.

    They are the filter plugins' callers and the roles that install a shared module from
    `hosts/lib/` or `scripts/lib/` (H7.2e). Read from this module's globals, like the others.
    """
    callers = globals()["SETUP_ROLES_CALLING_FILTER_PLUGINS"].get(path, ())
    return {*callers, *globals()["SETUP_FILES_SHIPPED_BY_OTHER_ROLES"].get(path, ())}


def rendered_only_by_host_packages(path: str) -> bool:
    """Whether `path` is a setup-tree file no setup role applies, only a host package.

    Read from this module's globals, so a copy `use_tables` adopted answers.
    """
    return path in globals()["SETUP_FILES_RENDERED_ONLY_BY_HOST_PACKAGES"]


def owner_routed_consumers(owner: str) -> frozenset[str]:
    """The roles that render a file of `owner`'s that `SETUP_FILES_ROUTED_TO_OWNER` records.

    Read from this module's globals, so a copy `use_tables` adopted answers. A copy that
    predates #4316 holds a frozenset of paths, with no consumers to read, and answers none.
    """
    table = globals()["SETUP_FILES_ROUTED_TO_OWNER"]
    if not isinstance(table, dict):
        return frozenset()
    prefix = f"ansible/roles/setup/{owner}/"
    return frozenset(
        role
        for path, roles in table.items()
        if path.startswith(prefix)
        for role in roles
    )


def use_tables(tables: dict[str, object]) -> None:
    """Rebind this module's tables for the rest of the process.

    A reader that looks a table up as `deploy_cross_role.<NAME>` at call time sees the new
    one. A name bound by `from deploy_cross_role import` keeps the old.
    """
    globals().update({name: tables[name] for name in TABLE_NAMES})
