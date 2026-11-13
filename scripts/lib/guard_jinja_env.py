#!/usr/bin/env python3
"""The Jinja environment every render guard uses, carrying the Ansible-dialect filters.

Each guard used to build its own environment over ``render_guard.make_env`` and register a
different subset: the compose guard had ``hash``, the shell guard had ``search`` and a
hand-written ``bool``, the setup guard had the real ``bool``/``comment``/``to_uuid``/
``mandatory``, the k8s guard had ``bool``/``hash``/``to_json`` plus the repo's two filter
plugins, and the unit and config guards had none. A template rendered clean under one guard
and failed under another for no reason a reader could derive from the template (#2408).

This module registers ONE set: ``renderer_lib/jinja_filters.py``'s reimplementations and this repo's
own filters in ``renderer/template_filters/``, each wrapped by ``as_filter``. ``to_json`` is ``k8s_yaml.to_json_stub``, because a guard renders secrets
as ``StubUndefined``, and plain-data JSON would abort the render this module exists to
complete. The renderer's own environment replaces it with ``filters.to_json``.

It re-exports ``render_guard.make_env``, the one environment factory, so a caller builds a
filtered environment as ``register_ansible_filters(make_env(...))`` from one import.

The module has two tiers. Its TOP LEVEL is the light tier, and carries ``ansible_bool``
for ``lib.k8s_context``. ``register_ansible_filters`` is the heavy tier, and imports the
filter modules when it is first called. Nothing here loads ansible-core.
``test_the_light_tier_loads_no_ansible_core`` pins that.

Imported as ``from lib.guard_jinja_env import ...`` after the caller's own ``sys.path``
bootstrap puts ``scripts/`` on the path (``.claude/rules/python-layout.md``).
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import functools
from pathlib import Path
from types import FunctionType
from typing import Any, cast

from jinja2 import Environment

from lib.k8s_yaml import to_json_stub
from lib.render_guard import (
    SHARED_TPL,
    StubUndefined,
    make_env,
    render_or_error,
)

__all__ = [
    "BOOLEANS_FALSE",
    "BOOLEANS_TRUE",
    "FilterError",
    "ansible_bool",
    "as_filter",
    "file_lookup",
    "make_env",
    "register_ansible_filters",
    "repo_filters",
    "render_template",
    "template_env",
]


# ansible-core 2.22's `to_bool` tables.
BOOLEANS_TRUE = frozenset({"yes", "true", "1", "on"})
BOOLEANS_FALSE = frozenset({"no", "false", "0", "off"})


def ansible_bool(value) -> bool:
    """Mirror Ansible's `bool` Jinja filter, `ansible.plugins.filter.core.to_bool`.

    The filter every environment registers, the renderer's and the guards' alike. It keeps
    `to_bool`'s tables because the templates' `| bool` was written against them: a string such
    as `"false"` reads as False, where Python's bool() and plain Jinja truthiness read any
    non-empty string as True. A stub that called bool() would agree on real booleans and
    disagree on exactly the strings the filter exists for.

    The filter is `to_bool`, NOT `module_utils.parsing.convert_bool.boolean` — that one serves
    module argument parsing and accepts `t`, `y`, `f`, `n` and any non-zero int. Mirroring
    `boolean` would render `'t'`, `'y'`, `2`, `-1` and `' True '` as True under the guard and
    False in the renderer. `to_bool` lowercases without stripping, stringifies
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


class FilterError(ValueError):
    """A filter that refused its input. The message opens with the filter's name.

    The renderer reads it as the `ValueError` it is, so a caller that catches `ValueError`
    still sees the refusal.
    """


def as_filter(func: FunctionType) -> FunctionType:
    """`func` as a template calls it: a `ValueError` re-raised as `FilterError`.

    The message opens with `<func's name>: `, which is the name the filter registers under, so
    a render failure names the filter that refused rather than only the template (#4480). A
    `FilterError` from a filter `func` calls passes through unprefixed.
    """

    @functools.wraps(func)
    def wrapped(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except FilterError:
            raise
        except ValueError as exc:
            raise FilterError(f"{func.__name__}: {exc}") from exc

    return cast(FunctionType, wrapped)


def repo_filters() -> dict[str, FunctionType]:
    """This repo's own filters, keyed by the function's name, which templates call them by.

    Each is wrapped by `as_filter`. The modules load on the first call, so the light tier
    above imports none of them.
    """
    from renderer.template_filters import (
        authelia_access,
        entry_fields,
        flux_components,
        grafana_dashboards,
        homepage_tiles,
        ingress_probes,
        longhorn_groups,
        role_label,
        scrape_jobs,
        service_tier,
    )

    funcs: tuple[FunctionType, ...] = (
        authelia_access.authelia_service_rules,
        entry_fields.entry_names,
        entry_fields.lan_hostnames,
        entry_fields.published_ports,
        flux_components.flux_components,
        grafana_dashboards.grafana_deploy_annotated,
        homepage_tiles.homepage_href,
        homepage_tiles.homepage_widget_url,
        homepage_tiles.netpol_caller_fences,
        homepage_tiles.netpol_callers,
        ingress_probes.ingress_probe_targets,
        longhorn_groups.backup_group_label,
        longhorn_groups.longhorn_backup_name,
        longhorn_groups.longhorn_nobackup_claims,
        longhorn_groups.longhorn_weekly_claims,
        longhorn_groups.weekly_backup_group,
        longhorn_groups.weekly_backup_shard,
        role_label.homelab_role_label,
        scrape_jobs.entry_port,
        scrape_jobs.metrics_port,
        scrape_jobs.scrape_jobs,
        service_tier.in_service_tier,
        service_tier.tier_backup_claims,
        service_tier.tier_priority_class,
    )
    return {func.__name__: as_filter(func) for func in funcs}


def register_ansible_filters(env: Environment) -> Environment:
    """Register on ``env`` the Ansible filters and tests this repo's templates reach for.

    ``bool`` is ``ansible_bool`` rather than Python's ``bool()``: ``bool("false")`` is True,
    so Python's would render ``{% if x | bool %}`` over a string the opposite way from what
    the template was written for, the divergence ``| bool`` is written to prevent.

    ``to_uuid`` is a deterministic UUIDv5, so a stub would render a different file each run.
    ``mandatory`` returns its input, so a guard's tracking Undefined passes through it and is
    judged by name as usual.

    ``authelia_service_rules`` and ``tier_priority_class`` are this repo's own template filters:
    authelia's config Secret derives its per-service access_control rules through the first,
    which raises on a ``use_authelia: true`` entry with no ``auth_tier``. A tiered role's pods
    take their PriorityClass from the second, which raises on an entry
    with no ``tier``. observability's blackbox probe jobs ask ``in_service_tier`` whether a
    service's target pages critical, and it raises on an entry name it cannot find. observability's
    Prometheus config renders its plain scrape jobs through ``scrape_jobs``, and each owning
    role reads its exporter's port through ``metrics_port``; both raise on a malformed
    ``metrics`` item. Its hand-written jobs read their entry's own port through ``entry_port``,
    which raises on an entry name it cannot find. observability's ingress probe targets derive
    from ``ingress_probe_targets``, which raises on a malformed ``probe`` key. Every caller fence
    reads its callers through ``netpol_callers``, and netpol-baseline's fences come from
    ``netpol_caller_fences``. artifacts, monitor-bridge and pihole collect names,
    ports and hostnames through ``entry_fields``' filters. homepage's tiles build their links and
    ClusterIP widget URLs through ``homepage_href`` and ``homepage_widget_url``, which raise on
    an entry that lacks the route or the ``homepage_widget`` key.
    The k3s RecurringJob template names each weekly shard through ``weekly_backup_group``, which
    raises on a shard outside 0-6, and the restore drill
    names the label prefix, the opt-out group and the B2 target through ``longhorn_backup_name``.
    longhorn-reconcile's ConfigMap computes the three tier lists with ``tier_backup_claims``,
    ``longhorn_weekly_claims`` and ``longhorn_nobackup_claims``, which raise on a shard outside
    0-6 or a claim two entries declare.
    The flux role fits its vendored install to the workload guards through ``flux_components``,
    which raises on a Deployment volume it has no rename for. observability's dashboard
    ConfigMaps inject the deploy-annotation queries into each board through
    ``grafana_deploy_annotated``, which raises on a board that is not a JSON object.
    Registering the real ones makes
    those failures reach the guard.

    ``homelab_role_label`` is the filter ``k8s/templates/role-labelled.yaml.j2`` stamps
    ``homelab/role`` with, for every role that arms the manifests role's prune.
    ``from_json`` is what home-assistant's ``templates/config/secrets.yaml.j2`` parses a
    service-account key with. ``homelab-render`` renders both templates, and its template
    census holds every filter and test name the k8s templates use to this set.
    ``from_yaml`` and ``regex_replace`` serve the role task expressions ``homelab-render``
    evaluates: image-builder's content tag parses its context render with the first.
    ``combine``: observability's ``manifests_self_rollouts`` adds each workload's namespace
    with it, and ``homelab-render`` evaluates that list to stamp ``checksum/restart`` on those
    workloads. ``basename`` and ``dirname`` serve the setup-role task expressions
    ``probe.py applied`` reads: claude_code's slice-cap prune gates on a ``basename``, and its
    homelab-ui and memory-store directories are a ``dirname`` (#6407, #6409).

    Args:
        env: The environment to register on, modified in place.

    Returns:
        ``env``, so a caller can build and register in one expression.
    """
    from renderer.renderer_lib import jinja_filters as filters

    env.filters["b64encode"] = filters.b64encode
    env.filters["basename"] = filters.basename
    env.filters["bool"] = ansible_bool
    env.filters["combine"] = filters.combine
    env.filters["comment"] = filters.comment
    env.filters["dirname"] = filters.dirname
    env.filters["from_json"] = filters.from_json
    env.filters["from_yaml"] = filters.from_yaml
    env.filters["regex_replace"] = filters.regex_replace
    # Ansible's `zip` filter is the builtin.
    env.filters["zip"] = zip
    env.filters["hash"] = filters.hash_text
    env.filters["mandatory"] = filters.mandatory
    env.filters["to_uuid"] = filters.to_uuid
    env.filters["to_json"] = to_json_stub
    env.filters.update(repo_filters())
    env.tests["search"] = filters.search
    return env


def file_lookup(kind: str, path: str, **_kwargs) -> str:
    """Ansible's ``lookup('file', path)``, the one lookup a setup-plane template may call.

    ``k8s_yaml.make_lookup`` covers the k8s guards' wider set, and replaces this global in the k8s render.

    Raises:
        ValueError: ``kind`` is not ``file``, since a stand-in for another plugin would render
            something a deploy does not.
    """
    if kind != "file":
        raise ValueError(f"lookup({kind!r}) has no stand-in in the setup-plane render")
    return Path(path).read_text().rstrip()


def template_env(template_dir: Path, undefined_cls=StubUndefined) -> Environment:
    """The environment for one template directory plus the shared ``k8s/templates/``.

    Every guard loads that pair — a role's own templates and the shared macros they import —
    so the pair lives here rather than in each guard's call.
    """
    env = register_ansible_filters(
        make_env([template_dir, SHARED_TPL], undefined_cls=undefined_cls)
    )
    # jinja2 leaves `Environment.globals` unannotated, so its inferred type rejects a callable.
    cast(dict[str, Any], env.globals)["lookup"] = file_lookup
    return env


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
