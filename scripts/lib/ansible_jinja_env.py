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

Importing ``ansible.plugins.filter.core`` costs ~190 ms, so this module is NOT the one a
latency-sensitive reader imports. ``lib.ansible_jinja_compat`` is that tier — hand-written
``bool`` and ``search`` shims, pinned to the real filters by
``validate/tests/test_validate_shell_templates.py``. ``lib.k8s_context`` uses it, for the
reason its own ``DECIDED:`` marker gives: ``probe_lib/monitors.py`` reaches that module and
``probe.py monitors`` loads no ansible-core today.

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

_sys.path.insert(0, str(ANSIBLE / "filter_plugins"))
from authelia_access import authelia_service_rules
from toposort import filter_by_platform

from ansible.plugins.filter.core import (
    comment,
    get_hash,
    mandatory,
    to_bool,
    to_uuid,
)
from ansible.plugins.test.core import search

__all__ = [
    "make_ansible_env",
    "register_ansible_filters",
    "render_template",
    "template_env",
]


def register_ansible_filters(env: Environment) -> Environment:
    """Register on ``env`` the Ansible filters and tests this repo's templates reach for.

    ``bool`` is ansible-core's ``to_bool`` rather than Python's ``bool()``: ``bool("false")``
    is True, and ``-e var=false`` arrives as the string, so a hand-rolled shim would render
    ``{% if x | bool %}`` the opposite way from a deploy — the exact divergence ``| bool``
    is written in a template to prevent.

    ``to_uuid`` is a deterministic UUIDv5, so a stub would render a different file each run.
    ``mandatory`` raises only on Ansible's own UndefinedMarker, so a guard's tracking
    Undefined passes through it and is judged by name as usual.

    ``filter_by_platform`` and ``authelia_service_rules`` are this repo's own filter plugins:
    pihole's ConfigMap derives its dnsmasq override records through the first, and authelia's
    config Secret derives its per-service access_control rules through the second, which
    raises on a ``use_authelia: true`` entry with no ``auth_tier``. Registering the real ones
    makes that failure reach the guard.

    Args:
        env: The environment to register on, modified in place.

    Returns:
        ``env``, so a caller can build and register in one expression.
    """
    env.filters["bool"] = to_bool
    env.filters["comment"] = comment
    env.filters["hash"] = get_hash
    env.filters["mandatory"] = mandatory
    env.filters["to_uuid"] = to_uuid
    env.filters["to_json"] = to_json_stub
    env.filters["filter_by_platform"] = filter_by_platform
    env.filters["authelia_service_rules"] = authelia_service_rules
    env.tests["search"] = search
    return env


def make_ansible_env(dirs, undefined_cls=StubUndefined) -> Environment:
    """``render_guard.make_env`` over ``dirs``, with the Ansible filters registered."""
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
