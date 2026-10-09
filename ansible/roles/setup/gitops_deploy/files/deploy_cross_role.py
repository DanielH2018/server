# ansible/roles/setup/gitops_deploy/files/deploy_cross_role.py
"""The files other roles ship, import or read by path, and the roles a change to one reaches.

`deploy_changes.services_from_changed_paths` and `setup_roles_for` read the setup tables below to
re-apply, or defer-and-alert, every role holding a copy of a changed file, and every setup
role calling a changed filter plugin's filters. They are static because every caller passes
paths alone, and `ansible/tests/setup/test_setup_cross_role_files.py` holds each one to the
tree.

The k8s plane needs no table: `k8s_lookup_readers` derives its readers from the templates and
tasks that name another role's file in a `lookup()`.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

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
_ALERT_UNIT = frozenset(
    {"claude_code", "gitops_deploy", "renovate_agent", "renovate_notify"}
)
SETUP_FILES_SHIPPED_BY_OTHER_ROLES: dict[str, frozenset[str]] = {
    f"{_COMMON}/tasks/alert_unit.yml": _ALERT_UNIT,
    f"{_COMMON}/templates/unit-failure-alert.service.j2": _ALERT_UNIT,
    f"{_COMMON}/templates/alert-webhook.env.j2": _ALERT_UNIT,
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
    "ansible/roles/setup/gitops_deploy/files/gitops_hold.py": frozenset(
        {"deploy_ui", "renovate_agent"}
    ),
}


# DECIDED: `common/templates/resolv.conf.j2` stays out of the table above and records `common`
# (#3319). `k3s` and `optimize_pi` render it directly, and neither is the tick's to apply:
# `k3s` runs only from `k3s-bringup.yml`, and `optimize_pi` only on daniel-pi
# (`gitops_markers.SETUP_ROLES_OFF_THE_TICK_HOST`, #3933). Recording `common` prints the
# two-host remediation in `deploy_remediation._setup_commands` as one line.
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


# A filter plugin mapped to the setup roles whose rendered state calls one of its filters
# (#3874). `ansible/deploy.yml` runs no setup role, so neither a narrowed deploy plane nor the
# full play it falls back to re-renders these. `setup_roles_for` returns them, and the tick
# applies or records each as it would its own change: `gitops_deploy` through
# `initial_setup.yml`, and `k3s` as a `manual_plane` line, since only `k3s-bringup.yml` runs it.
# `ansible/tests/setup/test_setup_cross_role_files.py` holds it to the tree.
SETUP_ROLES_CALLING_FILTER_PLUGINS: dict[str, frozenset[str]] = {
    "ansible/filter_plugins/k8s_autodeploy.py": frozenset({"gitops_deploy"}),
    "ansible/filter_plugins/longhorn_groups.py": frozenset({"k3s"}),
    "ansible/filter_plugins/service_tier.py": frozenset({"k3s"}),
}


# This module, by its repo path, and the names of the tables above.
CROSS_ROLE_FILE = "ansible/roles/setup/gitops_deploy/files/deploy_cross_role.py"
TABLE_NAMES = (
    "K8S_ROLES_IMPORTING_SETUP_FILES",
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


def use_tables(tables: dict[str, object]) -> None:
    """Rebind this module's tables for the rest of the process.

    A reader that looks a table up as `deploy_cross_role.<NAME>` at call time sees the new
    one. A name bound by `from deploy_cross_role import` keeps the old.
    """
    globals().update({name: tables[name] for name in TABLE_NAMES})


# A `lookup('file' | 'template', playbook_dir ~ '/roles/k8s/<role>/<path>')` naming one file,
# with `+` or `~` as the join. Group 1 is the path from `roles/`. A lookup that builds its path
# from a variable (`'/roles/k8s/x/files/' + name`) names a directory, never a file, and every
# such lookup in the tree reads its own role.
_K8S_LOOKUP_RE = re.compile(
    r"lookup\(\s*'(?:ansible\.builtin\.)?(?:file|template)'\s*,\s*playbook_dir\s*[+~]\s*"
    r"'/(roles/k8s/[^']+)'\s*\)"
)


def k8s_lookup_map(repo_root) -> dict[str, set[str]]:
    """Every k8s file another role `lookup()`s, from `roles/`, mapped to the roles reading it.

    A role reading its own file is left out. `k8s_lookup_readers` asks this of a change, and
    `scripts/secrets_mgmt/consumers.py` asks it of the whole tree, so a secret a reader renders
    out of another role's file credits that reader too.
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


def k8s_lookup_readers(paths, repo_root) -> set[str]:
    """k8s roles whose templates or tasks `lookup()` a changed file owned by a DIFFERENT role.

    A k8s role's change deploys that role, which is wrong for a file another role renders
    too. uptime-kuma renders a push tile for every row of monitor-bridge's
    `files/check_table.py` (#3781), and authelia and traefik embed crowdsec's allowlist files,
    so a change there must reach the readers as well as the owner. Derived by reading the
    lookups rather than listing the pairs, so a new reader is found the day it is written.
    The `import` form of the same question is `deploy_changes.shared_module_consumers`.

    Args:
        paths: The changed repo paths.
        repo_root: The checkout whose role trees are read.

    Returns:
        Only the EXTRA roles; a reader that is also the file's owner is left out.
    """
    changed = {
        p.removeprefix("ansible/") for p in paths if p.startswith("ansible/roles/k8s/")
    }
    if not changed:
        return set()
    lookups = k8s_lookup_map(repo_root)
    return {reader for target in changed for reader in lookups.get(target, ())}
