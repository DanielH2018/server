#!/usr/bin/env python3
"""Ansible's variable semantics, reproduced for the k8s manifest render guard.

Split out of ``scripts/validate/k8s_manifests.py`` on 2026-09-04; that module re-exports every
name here, so an existing importer keeps working. These are the pieces that decide what a
manifest renders WITH — the `bool` filter's string vocabulary, the recursive expansion Ansible
does on a variable's value, a role's resolved defaults, and the precedence collision the
validator asserts is empty.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import re

from lib.render_guard import SHARED_TPL, load_yaml, make_env
from lib.repo_paths import K8S_ROLES

__all__ = [
    "ansible_bool",
    "colliding_default_keys",
    "resolve_vars",
    "role_defaults",
]


def ansible_bool(value) -> bool:
    """Ansible's `bool` filter: the strings Ansible treats as true, plus ordinary truthiness.

    `-e k8s_dry_run=true` reaches a play as the STRING "true", which is why the filter exists
    at all — and why "false" must map to False here rather than to non-empty-string truthiness.
    """
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "on", "1"}
    return bool(value)


# A value that is nothing but one `{{ expr }}` — no surrounding text, no second expression.
_PURE_TEMPLATE = re.compile(r"^\{\{\s*(?P<expr>[^{}]+?)\s*\}\}$")


def resolve_vars(values: dict, context: dict, passes: int = 5) -> dict:
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
    """
    env = make_env([SHARED_TPL])
    # `bool` is an Ansible filter, not a Jinja builtin, so a group_var that uses it renders
    # here as "No filter named 'bool'" — a render failure pointing at a variable that is
    # perfectly valid under Ansible. Shimmed for the same reason the compose guard shims
    # `hash` and the shell guard shims `search`; see make_env's docstring.
    env.filters["bool"] = ansible_bool

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
        """
        if isinstance(node, str):
            if "{{" not in node:
                return node
            pure = _PURE_TEMPLATE.match(node.strip())
            if pure:
                return env.compile_expression(pure.group("expr"))(**ctx)
            return env.from_string(node).render(ctx)
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


def role_defaults(role: str, base: dict) -> dict:
    return resolve_vars(load_yaml(K8S_ROLES / role / "defaults" / "main.yml"), base)


def colliding_default_keys(role_vars: dict, base: dict) -> set:
    """The keys a role's defaults and the inventory both define — which must be none.

    The render context below is built `{**base, **role_defaults(...)}`, so a role default
    outranks the group_vars and host_vars merged into `base`. Ansible's own precedence is the
    reverse: role defaults are the WEAKEST layer and host_vars beat them. A shared key therefore
    makes this validator render a value a deploy would never produce, and it passes — the
    manifest is still valid YAML and still schema-checks, just against the wrong number.

    Asserted rather than fixed by swapping the merge order: swapping changes the context of all
    54 roles at once to correct a collision that does not exist today, where failing loudly
    costs nothing until one appears. `crowdsec_k8s_image` was hoisted into all.yml exactly this
    way once, so the hoist that creates one is a real move, not a hypothetical.
    """
    return set(role_vars) & set(base)
