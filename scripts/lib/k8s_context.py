#!/usr/bin/env python3
"""Ansible's variable semantics, reproduced for the k8s manifest render guard.

These are the pieces that decide what a manifest renders WITH — the recursive expansion Ansible
does on a variable's value, and a role's resolved defaults. The layering of those into one
render context, in Ansible's precedence, is `lib.render_context`. The `bool` filter that expansion registers is
`lib.ansible_jinja_env.ansible_bool`, that module's light-tier copy, rather than the real
filters its `register_ansible_filters` gives every render guard.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import functools
import re

from jinja2 import StrictUndefined, Undefined

from lib.ansible_jinja_env import ansible_bool
from lib.render_guard import (
    SHARED_TPL,
    StubUndefined,
    make_env,
    pad_to_source_newlines,
)

__all__ = [
    "resolve_vars",
]

# The heavy-tier filters a role's DEFAULTS call, which `resolve_vars` must evaluate. The three
# Longhorn tier lists: `longhorn-reconcile`'s defaults derive them from `containers_list`
# (H1.7a). ansible-core's `basename` and `dirname`: k3s's `k3s_host_coredns_tarball` is a
# `dirname`, and it rendered as STUB, with a warning, on every `probe.py applied k3s` (#6475).
#
# DECIDED: lazy, not registered with `register_ansible_filters` at the top of this module. The
# heavy tier imports ansible-core, and this module stays light for `probe.py monitors` (the
# `DECIDED:` marker in `resolve_vars` below gives the measurement). A wrapper here costs nothing
# until a default calls the filter, and then the call builds the heavy filter table once.
_DEFAULTS_PLUGIN_FILTERS = (
    "tier_backup_claims",
    "longhorn_weekly_claims",
    "longhorn_nobackup_claims",
    "basename",
    "dirname",
)


@functools.cache
def _heavy_filters() -> dict:
    """The filter table `register_ansible_filters` builds, imported on first use."""
    from jinja2 import Environment

    from lib.ansible_jinja_env import register_ansible_filters

    return register_ansible_filters(Environment()).filters


def _lazy_plugin_filter(name: str):
    def call(*args, **kwargs):
        return _heavy_filters()[name](*args, **kwargs)

    return call


# A value that is nothing but one `{{ expr }}` — no surrounding text, no second expression.
_PURE_TEMPLATE = re.compile(r"^\{\{\s*(?P<expr>[^{}]+?)\s*\}\}$")


def resolve_vars(
    values: dict, context: dict, passes: int = 5, undefined_cls=StubUndefined
) -> dict:
    """Expand ``{{ ... }}`` inside variable VALUES, the way Ansible does before templating.

    Ansible resolves a variable's value recursively, so a role default like
    ``n8n_k8s_image: "{{ k8s_registry_pull_host }}/n8n:latest"`` reaches a manifest already
    expanded — and ``k8s_registry_pull_host`` is itself ``"localhost:{{ k8s_registry_port }}"``,
    so one substitution is not enough. Loading the YAML raw expands nothing, and the literal
    braces survive into the rendered manifest, where ``{`` opens a flow mapping and the document
    fails to parse. That surfaces as "invalid YAML" pointing at a perfectly good template, which
    is precisely the diagnosis this guard exists to give correctly.

    Bounded rather than looped-to-fixpoint so a self-referential value fails the render with a
    recursion the operator can see, instead of hanging CI.

    ``undefined_cls`` decides what an undefined name inside a value becomes. The default
    ``StubUndefined`` renders ``STUB`` inside text and None as a whole value. A strict class
    such as ``render_guard.StrictChainableUndefined`` raises in both places instead.

    A value rendered as text ends in as many newlines as ansible-core gives it: Jinja drops the
    source's last one, and ``pad_to_source_newlines`` adds back only those the render lacks. So
    ``"p {{ x }}\\n"`` with ``x`` ending in a newline resolves to one newline, not two (#4853).
    """
    env = make_env([SHARED_TPL], undefined_cls=undefined_cls)
    env.keep_trailing_newline = False
    # `bool` is an Ansible filter, not a Jinja builtin, so a group_var that uses it renders
    # here as "No filter named 'bool'" — a render failure pointing at a variable that is
    # perfectly valid under Ansible. Every render guard registers the real filter through
    # `register_ansible_filters`; this module is the one caller that cannot, for the reason below.
    #
    # DECIDED: the shim is `ansible_jinja_env.ansible_bool`, not ansible-core's `to_bool`
    # that `lib.ansible_jinja_env.register_ansible_filters` binds for every render guard.
    # `to_bool` would make the two paths agree by identity, but importing
    # `ansible.plugins.filter.core` costs ~190 ms and `probe_lib/monitors.py` imports this
    # module — `probe.py monitors` reaches no ansible-core module today (measured 2026-09-18,
    # `python -X importtime`). The shim copies `to_bool`'s tables and fallback, and
    # `test_ansible_bool_agrees_with_ansible_core_to_bool` pins the copy to the real filter,
    # so the two paths agree by test rather than by identity (#2074).
    env.filters["bool"] = ansible_bool
    for name in _DEFAULTS_PLUGIN_FILTERS:
        env.filters[name] = _lazy_plugin_filter(name)

    def expand(node, ctx):
        """Recursively render every `{{ ... }}` string in `node`, not just a top-level one.

        Ansible templates a variable's value wherever a string sits inside it, not only when
        the whole value IS a string. A list- or dict-valued variable holding `{{ ... }}`
        therefore reaches a template already expanded; scanning only top-level strings would
        leave the literal braces in place one level further down.

        A value that is NOTHING BUT a single `{{ expr }}` is evaluated as a Jinja expression
        rather than rendered to text, so it keeps `expr`'s own type — this is what lets a role
        default alias a list- or bool-valued group_var (`netpol_baseline_node_cidrs: "{{
        k3s_cni0_gateways }}"`) rather than repeating its literal. Ansible does the same:
        confirmed against a live `ansible-playbook` run, a whole-value alias to a list
        variable comes back a list, not its `str()`. `.render()` always returns a string, so
        without this a list alias reaches a `{% for %}` loop as the literal characters of its
        Python repr — `[`, `'`, `1`, ... — silently breaking the template it aliases into.

        The shape is judged as Jinja parses it. One trailing newline still counts as a single
        expression, because Jinja drops it before parsing, and a string result is then padded
        like a text render. Any other text around the expression, such as a leading space or a
        second newline, makes the value text, as it is under Ansible.
        """
        if isinstance(node, str):
            if "{{" not in node:
                return node
            pure = _PURE_TEMPLATE.fullmatch(node.removesuffix("\n"))
            if pure:
                # `compile_expression` would turn an undefined result into None, which a
                # strict caller must see raise instead.
                value = env.compile_expression(
                    pure.group("expr"), undefined_to_none=False
                )(**ctx)
                if isinstance(value, StrictUndefined):
                    value._fail_with_undefined_error()
                if isinstance(value, Undefined):
                    return None
                if isinstance(value, str):
                    return pad_to_source_newlines(node, value)
                return value
            return pad_to_source_newlines(node, env.from_string(node).render(ctx))
        if isinstance(node, list):
            return [expand(n, ctx) for n in node]
        if isinstance(node, dict):
            return {k: expand(v, ctx) for k, v in node.items()}
        return node

    resolved = dict(values)
    for _ in range(passes):
        pending = {k: v for k, v in resolved.items() if "{{" in str(v)}
        if not pending:
            break
        for key, value in pending.items():
            resolved[key] = expand(value, {**context, **resolved})
    return resolved
