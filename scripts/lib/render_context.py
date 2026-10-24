#!/usr/bin/env python3
"""The variable context a role template renders with, in Ansible's precedence.

Every render guard under ``scripts/validate/`` builds its context here. Before #3692 five
validators each layered the base stubs, ``group_vars/all.yml``, host_vars and role defaults
themselves, in three different orders, and a validator that renders with the wrong precedence
checks a value no deploy produces (failure class 2 in ``docs/failure-classes.md``).

The layers, weakest first::

    BASE_CONTEXT stubs < role defaults < all.yml < host_vars < playbook_dir < overrides

Role defaults are Ansible's weakest real layer, so an inventory key of the same name wins.
``homelab-render host`` renders a component of a host package, which has no role, and passes
its ``component.yml`` ``defaults:`` map as ``component_defaults``, at the role defaults' place.
``overrides`` is what a caller supplies that no file holds: a stub a linter needs to be
plausible, a value a caller role passes to a shared template, a ``container_item``.

Typical usage example:

    ctx = render_context(path, overrides={"container_item": entry})
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from pathlib import Path

# The layers and where they live are lib.estate's, which the docs generators read through too,
# so a template render and a docs page cannot disagree about which value wins (#3899).
from lib.cli_help import warn
from lib.estate import (
    PLANE_HOSTS,
    REPO_INVENTORY,
    Inventory,
    inventory_layers,
    role_defaults,
)
from lib.k8s_context import resolve_vars
from lib.render_guard import (
    ANSIBLE,
    BASE_CONTEXT,
    StubUndefined,
    host_files,
    load_yaml,
)

__all__ = [
    "PLANE_HOSTS",
    "REPO_INVENTORY",
    "Inventory",
    "UnresolvedVarsError",
    "render_context",
]

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
    path: Path | None,
    host: str | None = None,
    overrides: dict | None = None,
    *,
    strict: bool = False,
    inventory: Inventory = REPO_INVENTORY,
    host_vars: dict | None = None,
    base: dict = BASE_CONTEXT,
    undefined_cls=StubUndefined,
    component_defaults: dict | None = None,
    withhold: frozenset[str] = frozenset(),
) -> dict:
    """The resolved context a template under `path` renders with in a deploy, `overrides` on top.

    Args:
        path: A role template, laid out ``roles/<plane>/<role>/templates/<name>``, or the
            role directory itself. The role's ``defaults/main.yml`` is the weakest real
            layer, and its plane picks the host from `PLANE_HOSTS` when `host` is None. None
            for a render with no role, which then has no role defaults layer.
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
        base: The stubs under every file layer. A guard keeps ``BASE_CONTEXT``, so a name no
            file holds still renders. ``homelab-render`` passes ``{}``, because a stub such as
            ``k8s_built_image_tags`` would fill in an input its strict render must refuse.
        undefined_cls: What an undefined name inside a variable's value becomes. The default
            renders ``STUB``, or None for a whole-value alias. A strict class raises instead,
            and the variable is left undefined, so a template that reads it raises in turn.
            With `strict`, it is reported in `UnresolvedVarsError`.
        component_defaults: A host component's ``defaults:`` map, layered with the role
            defaults beneath ``all.yml``. A host_vars value of the same name therefore wins,
            which an ``overrides`` entry would not (H3's *Component variables*).
        withhold: Names removed from every layer before anything resolves. Host mode passes
            the key names of ``secrets.yml``. Under a strict `undefined_cls`, a value that
            reads a withheld name is dropped too.

    Returns:
        A fresh dict, safe for the caller to extend.

    Raises:
        UnresolvedVarsError: `strict` is set and a variable would not expand.
    """
    role_dir = None
    if path is not None:
        role_dir = path.parents[1] if path.parent.name == "templates" else path
        host = host or inventory.plane_hosts.get(role_dir.parent.name)
    overrides = overrides or {}
    raw = {
        **base,
        **inventory_layers(
            {
                **(role_defaults(role_dir) if role_dir else {}),
                **(component_defaults or {}),
            },
            load_yaml(inventory.all_vars),
            _host_layer(inventory, host) if host_vars is None else host_vars,
        ),
        "playbook_dir": str(ANSIBLE),
        "hostvars": _hostvars(inventory),
        **overrides,
    }
    raw = {key: value for key, value in raw.items() if key not in withhold}
    resolved, dropped = _resolve(raw, undefined_cls)
    if dropped and strict:
        raise UnresolvedVarsError(dropped)
    # Under a strict class a dropped key needs no note: a template that reads it stops the
    # render, as Ansible's lazy evaluation fails only the task that reads the variable.
    if undefined_cls is not StubUndefined:
        dropped = {}
    for key, reason in sorted(dropped.items()):
        note = f"{key} left undefined, renders as STUB: {reason}"
        # Once per process: every template of a role shares its defaults, so the same drop
        # would otherwise print once for each of them.
        if note not in _NOTED:
            _NOTED.add(note)
            warn(note)
    return {**resolved, **overrides}


def _host_layer(inventory: Inventory, host: str | None) -> dict:
    return load_yaml(inventory.host_vars / f"{host}.yml") if host else {}


# The host_vars `hostvars` carries for each host, each only while its value holds no Jinja.
_HOSTVARS_KEYS = ("server_ip", "containers_list", "compose_services")


def _hostvars(inventory: Inventory) -> dict[str, dict]:
    """Each host's literal ``server_ip``, ``containers_list`` and ``compose_services``, as Ansible's ``hostvars`` has them.

    ``group_vars``' ``k8s_pi_client_ip`` reads the Pi's address this way (#3719), and
    observability's ``alloy-pi`` scrape job reads the Pi's ``alloy`` port from its
    ``containers_list`` (#3860), so a strict render needs both. Those readers and monitor-bridge's
    and artifacts' read the Pi's ``compose_services`` beside it (H5.1). A value goes in only while it
    holds no Jinja: here it would resolve against the rendered host's context, so the Pi's
    ``ansible_host: "{{ server_ip }}"`` would come out as daniel-box's address. daniel-box's
    own ``containers_list`` templates its entries, so it stays out. A caller needing more
    passes its own ``hostvars``.
    """
    found = {}
    for path in host_files(inventory.host_vars):
        layer = load_yaml(path)
        literal = {
            key: layer[key]
            for key in _HOSTVARS_KEYS
            if key in layer and not _holds_jinja(layer[key])
        }
        if literal:
            found[path.stem] = literal
    return found


def _holds_jinja(value) -> bool:
    if isinstance(value, str):
        return "{{" in value or "{%" in value
    if isinstance(value, dict):
        return any(_holds_jinja(k) or _holds_jinja(v) for k, v in value.items())
    if isinstance(value, list):
        return any(_holds_jinja(v) for v in value)
    return False


def _resolve(raw: dict, undefined_cls=StubUndefined) -> tuple[dict, dict[str, str]]:
    """`raw` expanded against itself, and each key dropped because its expansion raised.

    Whole-dict first, which is the common case and one pass. Two setup defaults need the
    per-key fallback, each naming a filter `resolve_vars`' light-tier environment does not
    register (its `DECIDED:` marker says why it stays light):
    `gitops_deploy_k8s_autodeploy_denylist` reaches this repo's `k8s_autodeploy_denylist`
    plugin, and `k3s_host_coredns_tarball` reaches ansible-core's `dirname`. The three Longhorn
    tier lists call repo plugins too, and `resolve_vars` registers those lazily.

    A dropped key renders as `STUB` through `StubUndefined` rather than as literal braces,
    which is what a guard asserting on a render needs: braces in the output are the defect
    the render exists to remove, so they must never arrive from the context.
    """
    try:
        return resolve_vars(raw, raw, undefined_cls=undefined_cls), {}
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
                resolved |= resolve_vars(
                    {key: value}, {**raw, **resolved}, undefined_cls=undefined_cls
                )
            except Exception as exc:
                dropped[key] = str(exc)
                resolved.pop(key, None)
    return resolved, dropped
