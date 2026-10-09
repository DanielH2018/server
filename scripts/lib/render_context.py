#!/usr/bin/env python3
"""The variable context a role template renders with, in Ansible's precedence.

Every render guard under ``scripts/validate/`` builds its context here. Before #3692 five
validators each layered the base stubs, ``group_vars/all.yml``, host_vars and role defaults
themselves, in three different orders, and a validator that renders with the wrong precedence
checks a value no deploy produces (failure class 2 in ``docs/failure-classes.md``).

The layers, weakest first::

    BASE_CONTEXT stubs < role defaults < all.yml < host_vars < playbook_dir < overrides

Role defaults are Ansible's weakest real layer, so an inventory key of the same name wins.
``overrides`` is what a caller supplies that no file holds: a stub a linter needs to be
plausible, a value a caller role passes to a shared template, a ``container_item``.

Typical usage example:

    ctx = render_context(path, overrides={"container_item": entry})
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import sys
from dataclasses import dataclass, field
from pathlib import Path

from lib.k8s_context import resolve_vars
from lib.render_guard import ALL_VARS, ANSIBLE, BASE_CONTEXT, HOST_VARS, load_yaml
from lib.ansible_inventory import K8S_HOST

__all__ = [
    "PLANE_HOSTS",
    "REPO_INVENTORY",
    "Inventory",
    "UnresolvedVarsError",
    "render_context",
]

# The host whose host_vars a plane's templates render with when the caller names none. Every
# k8s role deploys from daniel-box's `containers_list`, so its host_vars are the k8s plane's.
# A setup role runs on several hosts and a Pi compose role on the host that lists it, so
# neither plane has one host: a setup template renders with the group layer the hosts share,
# and the compose validator passes each host it renders for.
PLANE_HOSTS = {"k8s": K8S_HOST}


@dataclass(frozen=True)
class Inventory:
    """Where the inventory layers live, so a test can point a render at a throwaway tree.

    Attributes:
        all_vars: The group_vars file every host shares.
        host_vars: The directory holding one ``<host>.yml`` per inventory host.
        plane_hosts: The host each plane renders with when the caller names none.
    """

    all_vars: Path = ALL_VARS
    host_vars: Path = HOST_VARS
    plane_hosts: dict[str, str] = field(default_factory=lambda: dict(PLANE_HOSTS))


REPO_INVENTORY = Inventory()

_NOTED: set[str] = set()


class UnresolvedVarsError(ValueError):
    """A variable's value would not expand, under a caller that asked for a strict context.

    Attributes:
        keys: Each variable that would not expand, mapped to the reason.
    """

    def __init__(self, keys: dict[str, str]) -> None:
        super().__init__(
            "; ".join(f"{key}: {reason}" for key, reason in sorted(keys.items()))
        )
        self.keys = keys


def render_context(
    path: Path,
    host: str | None = None,
    overrides: dict | None = None,
    *,
    strict: bool = False,
    inventory: Inventory = REPO_INVENTORY,
    host_vars: dict | None = None,
) -> dict:
    """The resolved context a template under `path` renders with in a deploy, `overrides` on top.

    Args:
        path: A role template, laid out ``roles/<plane>/<role>/templates/<name>``, or the
            role directory itself. The role's ``defaults/main.yml`` is the weakest real
            layer, and its plane picks the host from `PLANE_HOSTS` when `host` is None.
        host: The inventory host whose host_vars go over ``all.yml``. None takes the plane's
            host, or no host_vars layer for a plane without one.
        overrides: Values laid over every file layer. They go in before anything resolves as
            well as on top after, so a default aliasing an overridden name (``x: "{{ secret
            }}"``) expands to the override rather than to the name's stub (#3191).
        strict: Raise rather than drop a variable whose value will not expand.
        inventory: Where all.yml and host_vars live; the repo's own unless a test says not.
        host_vars: The host layer as a mapping, read in place of the host's file. A test
            uses it to render a host the inventory does not hold, a key removed or a value
            changed. Unlike `overrides`, it resolves like the file it replaces: a value
            aliasing another (``x: "{{ sys_user }}"``) arrives expanded, never as braces.

    Returns:
        A fresh dict, safe for the caller to extend.

    Raises:
        UnresolvedVarsError: `strict` is set and a variable would not expand.
    """
    role_dir = path.parents[1] if path.parent.name == "templates" else path
    host = host or inventory.plane_hosts.get(role_dir.parent.name)
    overrides = overrides or {}
    raw = {
        **BASE_CONTEXT,
        **load_yaml(role_dir / "defaults" / "main.yml"),
        **load_yaml(inventory.all_vars),
        **(_host_layer(inventory, host) if host_vars is None else host_vars),
        "playbook_dir": str(ANSIBLE),
        **overrides,
    }
    resolved, dropped = _resolve(raw)
    if dropped and strict:
        raise UnresolvedVarsError(dropped)
    for key, reason in sorted(dropped.items()):
        note = f"  [note] {key} left undefined, renders as STUB: {reason}"
        # Once per process: every template of a role shares its defaults, so the same drop
        # would otherwise print once for each of them.
        if note not in _NOTED:
            _NOTED.add(note)
            print(note, file=sys.stderr)
    return {**resolved, **overrides}


def _host_layer(inventory: Inventory, host: str | None) -> dict:
    return load_yaml(inventory.host_vars / f"{host}.yml") if host else {}


def _resolve(raw: dict) -> tuple[dict, dict[str, str]]:
    """`raw` expanded against itself, and each key dropped because its expansion raised.

    Whole-dict first, which is the common case and one pass. Three setup defaults need the
    per-key fallback, each naming a filter `resolve_vars`' light-tier environment does not
    register (its `DECIDED:` marker says why it stays light):
    `gitops_deploy_k8s_autodeploy_denylist` and `k3s_longhorn_r2_volumes` reach this repo's
    `k8s_autodeploy_denylist` and `tier_backup_claims` plugins, and `k3s_host_coredns_tarball`
    reaches ansible-core's `dirname`.

    A dropped key renders as `STUB` through `StubUndefined` rather than as literal braces,
    which is what a guard asserting on a render needs: braces in the output are the defect
    the render exists to remove, so they must never arrive from the context.
    """
    try:
        return resolve_vars(raw, raw), {}
    except Exception:
        resolved: dict = {}
    dropped: dict[str, str] = {}
    # Twice over the dict, because a key resolved on its own sees only the keys already done:
    # a value referencing one later in `raw` would otherwise expand against a missing name
    # and come back carrying `STUB`. The second round re-resolves every surviving key with the
    # first round's results in context, so declaration order stops mattering.
    for _ in range(2):
        for key, value in raw.items():
            if key in dropped:
                continue
            try:
                resolved |= resolve_vars({key: value}, {**raw, **resolved})
            except Exception as exc:
                dropped[key] = str(exc)
                resolved.pop(key, None)
    return resolved, dropped
