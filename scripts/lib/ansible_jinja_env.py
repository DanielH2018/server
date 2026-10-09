#!/usr/bin/env python3
"""The Jinja environment every render guard uses, carrying ansible-core's own filters.

Each guard used to build its own environment over ``render_guard.make_env`` and register a
different subset: the compose guard had ``hash``, the shell guard had ``search`` and a
hand-written ``bool``, the setup guard had the real ``bool``/``comment``/``to_uuid``/
``mandatory``, the k8s guard had ``bool``/``hash``/``to_json`` plus the repo's two filter
plugins, and the unit and config guards had none. A template rendered clean under one guard
and failed under another for no reason a reader could derive from the template (#2408).

This module registers ONE set, and every filter but ``to_json`` is the implementation
ansible-core or this repo's ``filter_plugins/`` ships, so a guard agrees with a deploy by
identity rather than by a shim's fidelity. ``to_json`` is ``k8s_yaml.to_json_stub``:
ansible-core's raises on the ``StubUndefined`` a guard renders secrets as, which would abort
the render this module exists to complete.

Importing ``ansible.plugins.filter.core`` costs 100-365 ms (``python -X importtime``, warm
and cold runs on 2026-10-09), so the module has two tiers. Its TOP LEVEL is the
light tier: it loads no ansible-core, and carries ``ansible_bool``, a copy of ``to_bool`` for
``lib.k8s_context``. That module cannot pay the import, for the reason its own ``DECIDED:``
marker gives: ``probe_lib/monitors.py`` reaches it and ``probe.py monitors`` loads no
ansible-core. ``register_ansible_filters`` is the heavy tier, and imports ansible-core and the
repo's filter plugins when it is first called. ``test_the_light_tier_loads_no_ansible_core``
pins the split.

Imported as ``from lib.ansible_jinja_env import ...`` after the caller's own ``sys.path``
bootstrap puts ``scripts/`` on the path (``.claude/rules/python-layout.md``).
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from pathlib import Path

from jinja2 import Environment

from lib.k8s_yaml import to_json_stub
from lib.render_guard import (
    ANSIBLE,
    SHARED_TPL,
    StubUndefined,
    make_env,
    render_or_error,
)

__all__ = [
    "BOOLEANS_FALSE",
    "BOOLEANS_TRUE",
    "ansible_bool",
    "make_ansible_env",
    "register_ansible_filters",
    "render_template",
    "template_env",
]


# `ansible.plugins.filter.core._valid_bool_true` / `_valid_bool_false`, copied rather than
# imported so the light tier loads no ansible-core.
# `test_ansible_bool_agrees_with_ansible_core_to_bool` pins the copy.
BOOLEANS_TRUE = frozenset({"yes", "true", "1", "on"})
BOOLEANS_FALSE = frozenset({"no", "false", "0", "off"})


def ansible_bool(value) -> bool:
    """Mirror Ansible's `bool` Jinja filter, `ansible.plugins.filter.core.to_bool`.

    The light-tier copy, for ``lib.k8s_context`` only; every render guard gets the real
    ``to_bool`` from ``register_ansible_filters``. Faithfulness matters more than convenience:
    the reason templates use `| bool` at all is that `-e var=false` arrives as the STRING
    "false", which plain Jinja truthiness reads as True. A stub that just called Python's
    bool() would agree with Ansible on real booleans and disagree on exactly the inputs the
    filter exists for, so the test would pass while production took the other branch.

    The filter is `to_bool`, NOT `module_utils.parsing.convert_bool.boolean` — that one serves
    module argument parsing and accepts `t`, `y`, `f`, `n` and any non-zero int. Mirroring
    `boolean` would render `'t'`, `'y'`, `2`, `-1` and `' True '` as True under the guard and
    False in production. `to_bool` lowercases without stripping, stringifies
    ints (so `bool` lands in the tables), and coerces anything outside the tables with
    `value == 1` — a fallback it deprecates for removal in ansible-core 2.23. This mirrors the
    pinned filter's control flow, fallback included; the parity test flags the removal.
    """
    if isinstance(value, str):
        check = value.lower()
    elif isinstance(value, int):  # bool is an int
        check = str(value).lower()
    else:
        check = value
    try:
        if check in BOOLEANS_TRUE:
            return True
        if check in BOOLEANS_FALSE:
            return False
        return bool(check == 1)
    except TypeError:  # unhashable, e.g. a list or dict
        return False


def register_ansible_filters(env: Environment) -> Environment:
    """Register on ``env`` the Ansible filters and tests this repo's templates reach for.

    ``bool`` is ansible-core's ``to_bool`` rather than Python's ``bool()``: ``bool("false")``
    is True, and ``-e var=false`` arrives as the string, so a hand-rolled shim would render
    ``{% if x | bool %}`` the opposite way from a deploy — the exact divergence ``| bool``
    is written in a template to prevent.

    ``to_uuid`` is a deterministic UUIDv5, so a stub would render a different file each run.
    ``mandatory`` raises only on Ansible's own UndefinedMarker, so a guard's tracking
    Undefined passes through it and is judged by name as usual.

    ``filter_by_platform``, ``authelia_service_rules`` and ``tier_priority_class`` are this
    repo's own filter plugins: pihole's ConfigMap derives its dnsmasq override records through
    the first, and authelia's config Secret derives its per-service access_control rules
    through the second, which raises on a ``use_authelia: true`` entry with no ``auth_tier``.
    A tiered role's pods take their PriorityClass from the third, which raises on an entry
    with no ``tier``. uptime-kuma's static monitors ask ``in_service_tier`` whether a service's
    own tile pages by email, and it raises on an entry name it cannot find. ``py_table`` reads
    monitor-bridge's check table out of its Python source, for the env-secret and the Kuma tiles.
    Registering the real ones makes those failures reach the guard.

    Args:
        env: The environment to register on, modified in place.

    Returns:
        ``env``, so a caller can build and register in one expression.
    """
    # The heavy tier: imported here rather than at the top so the light tier above stays
    # free of ansible-core (see the module docstring).
    if str(ANSIBLE / "filter_plugins") not in _sys.path:
        _sys.path.insert(0, str(ANSIBLE / "filter_plugins"))
    from authelia_access import authelia_service_rules
    from py_table import py_table
    from service_tier import in_service_tier, tier_priority_class
    from toposort import filter_by_platform

    from ansible.plugins.filter.core import (
        comment,
        get_hash,
        mandatory,
        to_bool,
        to_uuid,
    )
    from ansible.plugins.test.core import search

    env.filters["bool"] = to_bool
    env.filters["comment"] = comment
    env.filters["hash"] = get_hash
    env.filters["mandatory"] = mandatory
    env.filters["to_uuid"] = to_uuid
    env.filters["to_json"] = to_json_stub
    env.filters["filter_by_platform"] = filter_by_platform
    env.filters["authelia_service_rules"] = authelia_service_rules
    env.filters["tier_priority_class"] = tier_priority_class
    env.filters["in_service_tier"] = in_service_tier
    env.filters["py_table"] = py_table
    env.tests["search"] = search
    return env


def make_ansible_env(dirs=(), undefined_cls=StubUndefined) -> Environment:
    """``render_guard.make_env`` over ``dirs``, with the Ansible filters registered.

    With no ``dirs`` the environment has an empty loader, which is the form a test takes to
    render a template's text or a task's expression through ``from_string``. That is still
    Ansible's whitespace flags and filters: a bare ``jinja2.Environment()`` has neither, and
    the ``tests-build-no-bare-jinja-environment`` row of
    ``scripts/tests/test_census_rows_test_renders.py`` refuses one in a test.
    """
    return register_ansible_filters(make_env(dirs, undefined_cls=undefined_cls))


def template_env(template_dir: Path, undefined_cls=StubUndefined) -> Environment:
    """The environment for one template directory plus the shared ``ansible/templates/``.

    Every guard loads that pair — a role's own templates and the shared macros they import —
    so the pair lives here rather than in each guard's call.
    """
    return make_ansible_env([template_dir, SHARED_TPL], undefined_cls=undefined_cls)


def render_template(path: Path, ctx: dict) -> str:
    """Render one template and return the text, RAISING RuntimeError if it will not render.

    The raising form is for a caller checking one template. A sweep over the tree wants every
    failure rather than the first, so it uses ``render_or_error`` directly and reports the
    string.

    Raises:
        RuntimeError: Carrying ``render_or_error``'s message, when the render fails.
    """
    rendered, err = render_or_error(template_env(path.parent), path.name, ctx)
    if rendered is None:
        raise RuntimeError(err)
    return rendered
