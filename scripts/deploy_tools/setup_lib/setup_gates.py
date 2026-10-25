#!/usr/bin/env python3
"""Read a playbook entry's `when:` for one host, the way Ansible would.

`land_reach` reads gates to say which hosts a setup-role change still owes, and fails wide
(`_eval_when`). `setup_routing` reads them to route the deployer's apply, and fails closed
(`eval_when_strict`), because a wide guess there is a `--tags` run that matches nothing
(#3933). One module so the two read a gate identically. `role_end_guard` and
`role_end_hosts` read a role's leading `meta: end_role` guard the same way for the
two-writer census, `land_reach` and the glance generator. `packaged_guard_hosts` reads the
`when:` of a task or an import that names a role's `<role>_packaged_hosts` map, with the role's
defaults layered under `group_vars` (docs/target-architecture/host/h7-delete-ansible.md,
*Moving one host at a time*).
"""

import functools
import sys
from collections.abc import Iterable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # scripts/

from lib import yaml_fast
from lib.ansible_inventory import inventory_hosts
from lib.json_types import JsonValue
from lib.repo_paths import ALL_VARS, HOST_VARS

# The hosts land.sh's setup-role remediation ever names: hosts.ini's `[homeservers]`, the
# group initial_setup.yml is run against. A staging host is excluded on purpose -- it is not
# land.sh's business (HOSTS_LAND_SH_NEVER_DEPLOYS in scripts/lib/render_guard.py is the same
# exclusion for a deploy tag), and it would sit in its own group rather than this one.
HOMESERVERS = [h for h in inventory_hosts() if "homeservers" in h.groups]
HOSTS = tuple(h.name for h in HOMESERVERS)


def _host_vars(
    host: str,
    all_vars: Path = ALL_VARS,
    host_vars_dir: Path = HOST_VARS,
    role_defaults: Path | None = None,
) -> dict:
    """`all_vars` overridden by `host_vars_dir`/<host>.yml, over `role_defaults` when given.

    Ansible's precedence puts a role's `defaults/main.yml` below `group_vars`, so it seeds the
    merge. A role with no defaults file seeds nothing.

    The same precedence Ansible resolves a `when:` variable through (a host_vars key always
    wins over the group default). Defaults to this repo's group_vars/all.yml and host_vars/.
    """
    merged = (
        dict(_vars_file(role_defaults))
        if role_defaults and role_defaults.exists()
        else {}
    )
    merged.update(_vars_file(all_vars))
    hv = host_vars_dir / f"{host}.yml"
    if hv.exists():
        merged.update(_vars_file(hv))
    return merged


def _vars_file(path: Path) -> dict:
    """`path` parsed once per content: the cache key carries its mtime, so a rewrite misses.

    `_eval_when` reads the merged vars per gate per host, and group_vars/all.yml is the
    largest YAML in the tree; without this, a 40-path setup-role note re-parsed it several
    hundred times.
    """
    return _parse_vars_file(path, path.stat().st_mtime_ns)


@functools.lru_cache(maxsize=32)
def _parse_vars_file(path: Path, _mtime_ns: int) -> dict:
    return yaml_fast.safe_load(path.read_text()) or {}


class GateUnreadable(Exception):
    """A `when:` value `eval_when_strict` cannot read for a host."""


def eval_when_strict(
    expr: object,
    host: str,
    all_vars: Path = ALL_VARS,
    host_vars_dir: Path = HOST_VARS,
    role_defaults: Path | None = None,
) -> bool:
    """A `when:` value for one host, read as Ansible would, or `GateUnreadable`.

    Every gate `initial_setup.yml` uses is a bare var, an `or`/`and` of them, an
    `inventory_hostname == <var-or-literal>` comparison, or one of those with a trailing
    `| bool` filter -- all valid Python once `| bool` is stripped, so `eval` against the
    host's merged vars reads them exactly as Ansible would. A YAML list is Ansible's
    implicit AND (`when: [a, b]` means `a and b`), so it is joined before evaluating rather
    than rejected. A value still holding Jinja (`{{`) would compare as its unrendered text,
    so it raises rather than reading False.

    Raises:
        GateUnreadable: a non-string, non-list value (a YAML `when: true`), an unresolved
            name, a Jinja construct `eval` cannot parse, or a var still holding Jinja.
    """
    if isinstance(expr, list):
        expr = " and ".join(f"({e})" for e in expr)
    if not isinstance(expr, str):
        raise GateUnreadable(f"`when: {expr!r}` is not an expression")
    ns = dict(_host_vars(host, all_vars, host_vars_dir, role_defaults))
    ns["inventory_hostname"] = host
    py_expr = expr.replace("| bool", "").replace("|bool", "")
    try:
        code = compile(py_expr, "<when>", "eval")
    except SyntaxError as exc:
        raise GateUnreadable(f"`when: {expr}` does not parse ({exc.msg})") from exc
    for name in code.co_names:
        if isinstance(ns.get(name), str) and "{{" in ns[name]:
            raise GateUnreadable(f"`{name}` is a Jinja expression on {host}")
    try:
        return bool(eval(code, {"__builtins__": {}}, ns))
    except Exception as exc:
        raise GateUnreadable(f"`when: {expr}` on {host}: {exc}") from exc


def _eval_when(
    expr: object, host: str, all_vars: Path = ALL_VARS, host_vars_dir: Path = HOST_VARS
) -> bool:
    """Best-effort read of a `when:` value for one host: `eval_when_strict`, or True.

    Returns True -- host REACHED -- whenever evaluation cannot be trusted. Wider than the
    truth is recoverable (an extra command an operator can no-op past); narrower silently
    hides a real gap, which is the failure this function exists to close. Same asymmetry
    `quiet_paths` already applies to a broad path it cannot read. The deployer's routing
    (`setup_routing`) takes the strict form, where a wide guess is the #3933 bug.
    """
    try:
        return eval_when_strict(expr, host, all_vars, host_vars_dir)
    except GateUnreadable:
        return True


_META_MODULES = ("ansible.builtin.meta", "meta")


def role_end_guard(role_dir: Path) -> JsonValue | None:
    """The `when:` of a role's leading `meta: end_role` task, or None when it has none.

    "Leading" is the first task of `tasks/main.yml`: Ansible ends the role for a host at that
    task, so every task after it skips there. A guard anywhere later ends the role only for
    the tasks that follow it, which no reader here claims. A leading `end_role` with no
    `when:` ends the role on every host, so it reads as `"True"`.

    A role excludes a host with this guard, and not with a `when:` on its `initial_setup.yml`
    entry, because an `initial_setup.yml` edit is in `_BROAD_MANUAL_PREFIXES` and parks every
    session's landing (docs/target-architecture/host/h4-secrets-unit.md, *The role guard after
    this slice*).
    """
    path = role_dir / "tasks" / "main.yml"
    if not path.is_file():
        return None
    tasks = yaml_fast.safe_load(path.read_text())
    if not isinstance(tasks, list) or not tasks or not isinstance(tasks[0], dict):
        return None
    first = tasks[0]
    if not any(first.get(module) == "end_role" for module in _META_MODULES):
        return None
    return first["when"] if "when" in first else "True"


def role_end_hosts(
    role_dir: Path,
    hosts: Iterable[str],
    all_vars: Path = ALL_VARS,
    host_vars_dir: Path = HOST_VARS,
) -> frozenset[str]:
    """The hosts of `hosts` on which the role's leading `end_role` guard passes.

    Each host is read with `eval_when_strict`. A guard it cannot read leaves the host out of
    the result, so the host stays reached: the same wide asymmetry `_eval_when` applies, since
    its callers say which hosts a role still owes, and a host dropped by a guess hides a gap.
    """
    guard = role_end_guard(role_dir)
    if guard is None:
        return frozenset()
    ended = set()
    for host in hosts:
        try:
            if eval_when_strict(guard, host, all_vars, host_vars_dir):
                ended.add(host)
        except GateUnreadable:
            continue
    return frozenset(ended)


PACKAGED_HOSTS = "_packaged_hosts"


def packaged_guard_hosts(
    whens: Iterable[object],
    hosts: Iterable[str],
    role_dir: Path,
    all_vars: Path = ALL_VARS,
    host_vars_dir: Path = HOST_VARS,
) -> frozenset[str]:
    """The hosts on which a `<role>_packaged_hosts` guard among `whens` is False.

    A task or an import carrying `when: inventory_hostname not in <role>_packaged_hosts[...]`
    skips on a host whose package owns the subject, so that host counts as not written by the
    task. Only a `when:` naming the map is read; every other condition reads a registered result
    or a runtime fact and is left as true, which keeps the answer wide. Each guard is read
    strictly, with the role's `defaults/main.yml` under `group_vars`, because a guess here would
    hide a second writer.

    Raises:
        GateUnreadable: a guard naming the map cannot be evaluated for a host.
    """
    exprs = [
        expr
        for when in whens
        for expr in (when if isinstance(when, list) else [when])
        if isinstance(expr, str) and PACKAGED_HOSTS in expr
    ]
    defaults = role_dir / "defaults" / "main.yml"
    return frozenset(
        host
        for host in hosts
        for expr in exprs
        if not eval_when_strict(expr, host, all_vars, host_vars_dir, defaults)
    )
