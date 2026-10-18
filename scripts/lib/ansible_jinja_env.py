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
from lib.repo_paths import FILTER_PLUGINS
from lib.render_guard import (
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
    repo's own filter plugins: ``deploy.yml`` splits each host's entries by platform through the
    first, and authelia's config Secret derives its per-service access_control rules
    through the second, which raises on a ``use_authelia: true`` entry with no ``auth_tier``.
    A tiered role's pods take their PriorityClass from the third, which raises on an entry
    with no ``tier``. uptime-kuma's static monitors ask ``in_service_tier`` whether a service's
    own tile pages by email, and it raises on an entry name it cannot find. observability's
    Prometheus config renders its plain scrape jobs through ``scrape_jobs``, and each owning
    role reads its exporter's port through ``metrics_port``; both raise on a malformed
    ``metrics`` item. Its hand-written jobs read their entry's own port through ``entry_port``,
    which raises on an entry name it cannot find. uptime-kuma's ingress tiles derive from
    ``kuma_ingress_monitors``, which raises on a malformed ``kuma`` key. Every caller fence
    reads its callers through ``netpol_callers``, and netpol-baseline's fences come from
    ``netpol_caller_fences``. artifacts, monitor-bridge, pihole and uptime-kuma collect names,
    ports and hostnames through ``entry_fields``' filters. homepage's tiles build their links and
    ClusterIP widget URLs through ``homepage_href`` and ``homepage_widget_url``, which raise on
    an entry that lacks the route or the ``homepage_widget`` key.
    ``py_table`` reads monitor-bridge's check table out of its Python source, for the
    env-secret and the Kuma tiles. The k3s RecurringJob template names each weekly shard
    through ``weekly_backup_group``, which raises on a shard outside 0-6, and the restore drill
    names the label prefix, the opt-out group and the B2 target through ``longhorn_backup_name``.
    The flux role fits its vendored install to the workload guards through ``flux_components``,
    which raises on a Deployment volume it has no rename for. observability's dashboard
    ConfigMaps inject the deploy-annotation queries into each board through
    ``grafana_deploy_annotated``, which raises on a board that is not a JSON object.
    Registering the real ones makes
    those failures reach the guard.

    ``homelab_role_label`` is the filter ``ansible/templates/role-labelled.yaml.j2`` stamps
    ``homelab/role`` with, for every role that arms the manifests role's prune.
    ``from_json`` is ansible-core's, which home-assistant's ``templates/config/secrets.yaml.j2``
    parses a service-account key with. ``homelab-render`` renders both templates, and its
    template census holds every filter and test name the k8s templates use to this set.
    ``from_yaml`` and ``regex_replace`` are ansible-core's too, for the role task expressions
    ``homelab-render`` evaluates: image-builder's content tag parses its context render with
    the first, and uptime-kuma's ``kuma_declarations`` names each declaration with the second
    and pairs names with declarations through ``zip``. ``combine`` is ansible-core's too:
    observability's ``manifests_self_rollouts`` adds each workload's namespace with it, and
    ``homelab-render`` evaluates that list to stamp ``checksum/restart`` on those workloads.

    Args:
        env: The environment to register on, modified in place.

    Returns:
        ``env``, so a caller can build and register in one expression.
    """
    # The heavy tier: imported here rather than at the top so the light tier above stays
    # free of ansible-core (see the module docstring).
    if str(FILTER_PLUGINS) not in _sys.path:
        _sys.path.insert(0, str(FILTER_PLUGINS))
    from authelia_access import authelia_service_rules
    import flux_components
    import grafana_dashboards
    from entry_fields import entry_names, lan_hostnames, published_ports
    import homepage_tiles
    import kuma_monitors
    import longhorn_groups
    import py_table
    import role_label
    import scrape_jobs
    import service_tier
    from toposort import filter_by_platform

    from ansible.plugins.filter.core import (
        b64encode,
        combine,
        comment,
        from_json,
        from_yaml,
        get_hash,
        mandatory,
        regex_replace,
        to_bool,
        to_uuid,
    )
    from ansible.plugins.test.core import search

    env.filters["b64encode"] = b64encode
    env.filters["bool"] = to_bool
    env.filters["combine"] = combine
    env.filters["comment"] = comment
    env.filters["from_json"] = from_json
    env.filters["from_yaml"] = from_yaml
    env.filters["regex_replace"] = regex_replace
    # ansible-core's `zip` filter is the builtin.
    env.filters["zip"] = zip
    env.filters["hash"] = get_hash
    env.filters["mandatory"] = mandatory
    env.filters["to_uuid"] = to_uuid
    env.filters["to_json"] = to_json_stub
    env.filters["filter_by_platform"] = filter_by_platform
    env.filters["authelia_service_rules"] = authelia_service_rules
    env.filters["flux_components"] = flux_components.FilterModule().filters()[
        "flux_components"
    ]
    # Through FilterModule, as Ansible loads them, so a refusal carries the filter's name.
    service_tier_filters = service_tier.FilterModule().filters()
    env.filters["tier_priority_class"] = service_tier_filters["tier_priority_class"]
    env.filters["in_service_tier"] = service_tier_filters["in_service_tier"]
    scrape_filters = scrape_jobs.FilterModule().filters()
    env.filters["scrape_jobs"] = scrape_filters["scrape_jobs"]
    env.filters["metrics_port"] = scrape_filters["metrics_port"]
    env.filters["entry_port"] = scrape_filters["entry_port"]
    env.filters["py_table"] = py_table.FilterModule().filters()["py_table"]
    env.filters["grafana_deploy_annotated"] = (
        grafana_dashboards.FilterModule().filters()["grafana_deploy_annotated"]
    )
    env.filters["homelab_role_label"] = role_label.FilterModule().filters()[
        "homelab_role_label"
    ]
    env.filters["kuma_ingress_monitors"] = kuma_monitors.FilterModule().filters()[
        "kuma_ingress_monitors"
    ]
    homepage_filters = homepage_tiles.FilterModule().filters()
    env.filters["netpol_callers"] = homepage_filters["netpol_callers"]
    env.filters["netpol_caller_fences"] = homepage_filters["netpol_caller_fences"]
    env.filters["entry_names"] = entry_names
    env.filters["published_ports"] = published_ports
    env.filters["lan_hostnames"] = lan_hostnames
    env.filters["homepage_widget_url"] = homepage_filters["homepage_widget_url"]
    env.filters["homepage_href"] = homepage_filters["homepage_href"]
    longhorn_filters = longhorn_groups.FilterModule().filters()
    for name in (
        "backup_group_label",
        "weekly_backup_group",
        "weekly_backup_shard",
        "longhorn_backup_name",
    ):
        env.filters[name] = longhorn_filters[name]
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
