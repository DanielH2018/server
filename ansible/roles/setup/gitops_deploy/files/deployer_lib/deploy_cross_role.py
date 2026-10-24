# ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_cross_role.py
"""The files other roles ship, import or read by path, and the roles a change to one reaches.

`deploy_changes.services_from_changed_paths` and `setup_roles_for` read the setup tables below to
re-apply, or defer-and-alert, every role holding a copy of a changed file, and every setup
role calling a changed filter plugin's filters. They are static because every caller passes
paths alone, and `ansible/tests/setup/test_setup_cross_role_files.py` holds each one to the
tree.

The k8s plane needs one table, `K8S_BUILD_CONTEXT_FILES`, for a file outside `ansible/roles/k8s/`
that a role's image embeds. Every other k8s reader is derived: `k8s_lookup_readers` reads the
templates and tasks that name another role's file in a `lookup()`.

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
# kuma-check pair reaches a host only through `kuma_check_timer.yml`.
# `ansible/tests/setup/test_setup_cross_role_files.py` holds it to the tree, and
# `test_setup_cross_role_reads.py` holds its `defaults/` rows.
_COMMON = "ansible/roles/setup/common"
_HOST_LIB = frozenset(
    {
        "fake_remux",
        "gitops_deploy",
        "homelab_builder",
        "k3s",
        "renovate_agent",
        "renovate_notify",
    }
)
_K3S = frozenset({"k3s"})
_LH = "scripts/lib/longhorn_lib"
_STAMPED = _HOST_LIB | {"claude_code", "deploy_ui", "initial_setup"}
_KUMA_CHECK = frozenset({"gitops_deploy", "initial_setup", "k3s", "render_records"})
_ALERT_UNIT = frozenset(
    {
        "claude_code",
        "gitops_deploy",
        "homelab_builder",
        "renovate_agent",
        "renovate_notify",
    }
)
SETUP_FILES_SHIPPED_BY_OTHER_ROLES: dict[str, frozenset[str]] = {
    f"{_COMMON}/tasks/alert_unit.yml": _ALERT_UNIT,
    f"{_COMMON}/templates/unit-failure-alert.service.j2": _ALERT_UNIT,
    f"{_COMMON}/templates/alert-webhook.env.j2": _ALERT_UNIT,
    f"{_COMMON}/tasks/agent_user.yml": frozenset({"claude_code", "renovate_agent"}),
    # `hosts/lib/` since H7.2e: the packages ship it, and the Ansible consumers below still copy
    # it beside their scripts until their own PRs move them. `ci_verdict.py` is the deployer's
    # alone (`gitops_deploy/tasks/code.yml` copies it), and the longhorn_lib modules are the
    # k3s role's backup-health reader's (`k3s/tasks/health-crons.yml` copies them).
    "hosts/lib/host_lib.py": _HOST_LIB,
    "hosts/lib/kuma_push.py": _HOST_LIB,
    "hosts/lib/ci_verdict.py": frozenset({"gitops_deploy"}),
    f"{_LH}/longhorn_backup_health_logic.py": _K3S,
    f"{_LH}/longhorn_backups.py": _K3S,
    f"{_LH}/longhorn_cluster_state.py": _K3S,
    f"{_LH}/longhorn_cron_evidence_logic.py": _K3S,
    f"{_LH}/longhorn_restore_drill_stamps.py": _K3S,
    f"{_LH}/longhorn_skipped_volumes_logic.py": _K3S,
    f"{_COMMON}/tasks/install_host_lib.yml": _HOST_LIB,
    f"{_COMMON}/tasks/kuma_check_timer.yml": _KUMA_CHECK,
    f"{_COMMON}/templates/kuma-check.service.j2": _KUMA_CHECK,
    f"{_COMMON}/templates/kuma-check.timer.j2": _KUMA_CHECK,
    f"{_COMMON}/templates/kuma-check-boot.service.j2": _KUMA_CHECK,
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
    # A `defaults/` file whose variable another role reads (#4303). claude_code puts each agent
    # in `initial_setup_worktree_holders_group`, so a rename re-applied only initial_setup and
    # moved the socket away from every agent. A change to that key applies the whole
    # claude_code tag: its reader, `tasks/agent_access.yml`, runs only inside the agent
    # include, whose tasks all carry that one tag. A change to keys claude_code never reads
    # drops claude_code from the tick's run (#4361). claude_code, renovate_agent and
    # homelab_builder each copy the uv binary `initial_setup_uv_version` pins, and name that key
    # where they check the copy, so a uv bump re-applies all three after initial_setup (#5945).
    "ansible/roles/setup/initial_setup/defaults/main.yml": frozenset(
        {"claude_code", "homelab_builder", "renovate_agent"}
    ),
    # initial_setup's worktree-holder crons read the agent list, the agent user, its primary
    # profile and the operator checkout from claude_code's defaults (#4357).
    "ansible/roles/setup/claude_code/defaults/main.yml": frozenset({"initial_setup"}),
    # deploy_ui's firewall sources include `k3s_agent_node_ips` (#4357). Only `k3s-bringup.yml`
    # runs k3s, so a change here records k3s in `manual_plane` and the tick applies deploy_ui.
    "ansible/roles/setup/k3s/defaults/main.yml": frozenset({"deploy_ui"}),
    # The host-package builder's settings file renders the deployer's CI gate, walk bound and
    # repo from these defaults (H3.5a), so a change to any of them re-renders the builder's copy.
    # H7.2e moves the CI keys into inventory and owes this row's replacement.
    "ansible/roles/setup/gitops_deploy/defaults/main.yml": frozenset(
        {"homelab_builder"}
    ),
}


# DECIDED: `common/templates/resolv.conf.j2` stays out of the table above and records `common`
# (#3319). `k3s` and `optimize_pi` render it directly, and neither is the tick's to apply:
# `k3s` runs only from `k3s-bringup.yml`, and `optimize_pi` only on daniel-pi, by its gate in
# `initial_setup.yml` (#3933). Recording `common` prints the
# two-host remediation in `deploy_remediation._setup_commands` as one line.
# The value names the roles that render the file. `_setup_commands` builds `common`'s command
# from them and the routing (#4316), so neither a host nor a playbook is written down twice.
SETUP_FILES_ROUTED_TO_OWNER: dict[str, frozenset[str]] = {
    f"{_COMMON}/templates/resolv.conf.j2": frozenset({"k3s", "optimize_pi"}),
}

# The k8s roles that import a setup file by path, so a change to it defer-and-alerts each one
# (#3320). The deployer never applies a k8s role for a change that is not an image-pin bump, so
# naming them in `cs.k8s` is what keeps their copy of `host_lib.py` from going stale unseen.
# `ansible/tests/setup/test_setup_cross_role_files.py` holds it to the tree.
# No k8s role copies host_lib.py onto a host since H2.5c: configarr's and janitorr's health
# readers stage host_lib.py and kuma_push.py into their CronJobs' ConfigMaps, so no k8s role
# imports install_host_lib.yml or stamp_deployed.yml any more.
K8S_ROLES_IMPORTING_SETUP_FILES: dict[str, frozenset[str]] = {
    "hosts/lib/host_lib.py": frozenset({"configarr", "janitorr"}),
    # autofix-bridge, configarr, crowdsec, janitorr and uptime-kuma stage it into a pod's
    # ConfigMap (#3745); crowdsec's is its crowdsec-jobs sidecar's, since H2.4b.
    "hosts/lib/kuma_push.py": frozenset(
        {"autofix-bridge", "configarr", "crowdsec", "janitorr", "uptime-kuma"}
    ),
}


# A file outside `ansible/roles/k8s/` that a k8s role's `image_builder_context_files` embeds in
# its image, mapped to those roles (#4840). image-builder hashes the build context into the
# content tag the Deployment names, so an edit here moves the role's applied bytes. The path
# sits under no role, so without this row it mapped to no deploy tag and wrote no
# `k8s_unapplied` line, and `probe.py releases` blamed whichever unrelated path also moved.
# A Flux-owned role still lands here first; `deploy_inventory.route_not_ansible_owned` then
# moves it to `cs.flux`. `ansible/tests/setup/test_setup_cross_role_files.py` holds it to the tree.
K8S_BUILD_CONTEXT_FILES: dict[str, frozenset[str]] = {
    "scripts/diagnostics/probe_lib/obs_api.py": frozenset({"homelab-mcp"}),
}


# A filter plugin mapped to the setup roles whose rendered state calls one of its filters
# (#3874). `ansible/deploy.yml` runs no setup role, so no deploy-plane apply, narrowed or
# whole, re-renders these. `setup_roles_for` returns them, and the tick
# applies or records each as it would its own change: `gitops_deploy` through
# `initial_setup.yml`, and `k3s` as a `manual_plane` line, since only `k3s-bringup.yml` runs it.
# `ansible/tests/setup/test_setup_cross_role_reads.py` holds it to the tree.
SETUP_ROLES_CALLING_FILTER_PLUGINS: dict[str, frozenset[str]] = {
    "ansible/filter_plugins/claude_agents.py": frozenset(
        {"claude_code", "initial_setup"}
    ),
    "ansible/filter_plugins/k8s_autodeploy.py": frozenset({"gitops_deploy"}),
    "ansible/filter_plugins/longhorn_groups.py": frozenset({"k3s"}),
    # Registers no filter; `longhorn_groups` and `service_tier` wrap theirs in it (#4527).
    "ansible/filter_plugins/filter_errors.py": frozenset({"k3s"}),
}


# This module, by its repo path, and the names of the tables above.
CROSS_ROLE_FILE = (
    "ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_cross_role.py"
)
TABLE_NAMES = (
    "K8S_BUILD_CONTEXT_FILES",
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


def k8s_roles_holding(path: str) -> frozenset[str]:
    """The k8s roles holding a copy of `path` from outside their own tree.

    That is a setup file one imports (`K8S_ROLES_IMPORTING_SETUP_FILES`) or a file its image
    embeds (`K8S_BUILD_CONTEXT_FILES`). Read from this module's globals, so a copy
    `use_tables` adopted answers.
    """
    return globals()["K8S_ROLES_IMPORTING_SETUP_FILES"].get(
        path, frozenset()
    ) | globals()["K8S_BUILD_CONTEXT_FILES"].get(path, frozenset())


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
