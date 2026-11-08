# ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_cross_role.py
"""The files other roles ship, import or read by path, and the roles a change to one reaches.

`deploy_changes.services_from_changed_paths` and `setup_roles_for` read the setup tables below to
re-apply, or defer-and-alert, every role holding a copy of a changed file, and every setup
role calling a changed filter plugin's filters. They are static because every caller passes
paths alone, and `ansible/tests/setup/test_setup_cross_role_files.py` holds each one to the
tree.

The k8s plane needs no table, because the renderer reads every file a role's render holds.
`k8s_lookup_map` reads the templates and tasks that name another role's file in a `lookup()`,
for the secret census.

A module of its own, not part of `deploy_changes`, because the tick and `land.sh` parse
another commit's copy of THIS file by path (`tables_in`, `CROSS_ROLE_FILE`). Nothing else may
share the file that parse reads.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

# A file one setup role installs from another's `files/`, imports from another's `tasks/`, or
# reads a variable of from another's `defaults/`, mapped to those roles, so a change to it re-applies them beside the owner (#3306, #3317).
# A template a shared task file renders inherits that task file's importers (#3319): the
# kuma-check pair reaches a host only through `check_timer.yml`.
# `ansible/tests/setup/test_setup_cross_role_files.py` holds it to the tree, and
# `test_setup_cross_role_reads.py` holds its `defaults/` rows.
_COMMON = "ansible/roles/setup/common"
_HOST_LIB = frozenset({"gitops_deploy"})
_STAMPED = _HOST_LIB | {"initial_setup"}
_CHECK_TIMER = frozenset({"gitops_deploy", "initial_setup"})
_ALERT_UNIT = frozenset({"gitops_deploy"})
SETUP_FILES_SHIPPED_BY_OTHER_ROLES: dict[str, frozenset[str]] = {
    f"{_COMMON}/tasks/alert_unit.yml": _ALERT_UNIT,
    f"{_COMMON}/templates/unit-failure-alert.service.j2": _ALERT_UNIT,
    f"{_COMMON}/templates/alert-webhook.env.j2": _ALERT_UNIT,
    # `hosts/lib/` since H7.2e: the packages ship it, and the Ansible consumers below still copy
    # it beside their scripts until their own PRs move them. `ci_verdict.py` is the deployer's
    # alone (`gitops_deploy/tasks/code.yml` copies it).
    "hosts/lib/host_lib.py": _HOST_LIB,
    "hosts/lib/ci_verdict.py": frozenset({"gitops_deploy"}),
    f"{_COMMON}/tasks/install_host_lib.yml": _HOST_LIB,
    f"{_COMMON}/tasks/check_timer.yml": _CHECK_TIMER,
    f"{_COMMON}/templates/check-timer.service.j2": _CHECK_TIMER,
    f"{_COMMON}/templates/check-timer.timer.j2": _CHECK_TIMER,
    f"{_COMMON}/templates/check-timer-boot.service.j2": _CHECK_TIMER,
    f"{_COMMON}/tasks/stamp_deployed.yml": _STAMPED,
    f"{_COMMON}/tasks/stamp_render.yml": _STAMPED,
}


# A setup-tree template that no setup role imports and one role renders by path, mapped to the
# roles that render it. Empty since H7.35: the host package builder renders
# `common/templates/resolv.conf.j2`, through the `pi-resolver` and `k3s-host-dns` components.
SETUP_FILES_ROUTED_TO_OWNER: dict[str, frozenset[str]] = {}

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


# A `lookup('file' | 'template', playbook_dir ~ '/roles/k8s/<role>/<path>')` naming one file,
# with `+` or `~` as the join and any keyword arguments after the path (`rstrip=False`, #5463).
# Group 1 is the path from `roles/`. A lookup that builds its path from a variable
# (`'/roles/k8s/x/files/' + name`) names a directory, never a file, and every such lookup in
# the tree reads its own role.
_K8S_LOOKUP_RE = re.compile(
    r"lookup\(\s*'(?:ansible\.builtin\.)?(?:file|template)'\s*,\s*playbook_dir\s*[+~]\s*"
    r"'/(roles/k8s/[^']+)'\s*(?:,[^)]*)?\)"
)


def k8s_lookup_map(repo_root) -> dict[str, set[str]]:
    """Every k8s file another role `lookup()`s, from `roles/`, mapped to the roles reading it.

    A role reading its own file is left out. `scripts/secrets_mgmt/consumers.py` asks this of
    the whole tree, so a secret a reader renders out of another role's file credits that
    reader too.
    """
    k8s = Path(repo_root) / "ansible" / "roles" / "k8s"
    found: dict[str, set[str]] = {}
    if not k8s.is_dir():
        return found
    for role in sorted(k8s.iterdir()):
        for src in [*role.glob("templates/**/*.j2"), *role.glob("tasks/*.yml")]:
            try:
                text = src.read_text(errors="ignore")
            except OSError:
                continue
            for target in _K8S_LOOKUP_RE.findall(text):
                if target.split("/")[2] != role.name:
                    found.setdefault(target, set()).add(role.name)
    return found
