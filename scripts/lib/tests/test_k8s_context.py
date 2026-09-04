#!/usr/bin/env python3
"""Ansible's variable semantics as the k8s render guard reproduces them.

`resolve_vars` expands `{{ ... }}` inside a variable's VALUE the way Ansible does before
templating; getting that wrong leaves literal braces in a manifest, which reads as "invalid
YAML" pointing at a perfectly good template. `colliding_default_keys` asserts the render
context's inverted precedence stays harmless.

Split out of scripts/validate/tests/test_validate_k8s_manifests.py on 2026-09-04, with the
code it covers.

Run: uv run pytest scripts/lib/tests/test_k8s_context.py
"""

from lib.k8s_context import colliding_default_keys, resolve_vars


# --- resolve_vars: values nested inside lists and dicts ---
#
# Ansible expands `{{ ... }}` wherever a string sits inside a variable's value, not only when
# the whole value IS a string. Scanning top-level strings alone left the braces in place one
# level down, so a list-valued variable reached a template unexpanded and rendered literal
# `{{ ... }}` into YAML — the same defect resolve_vars exists to prevent, one level deeper.
#
# `traefik_k8s_watched_namespaces` is the live case: a YAML list of namespace references,
# looped over by both rbac.yaml.j2 and static-config.yaml.j2.


def test_resolve_vars_expands_a_string_inside_a_list():
    resolved = resolve_vars(
        {"watched": ["{{ ns_a }}", "{{ ns_b }}"]},
        {"ns_a": "homelab", "ns_b": "longhorn"},
    )
    assert resolved["watched"] == ["homelab", "longhorn"]


def test_resolve_vars_expands_a_string_inside_a_dict():
    resolved = resolve_vars({"m": {"src": "{{ root }}/x.py"}}, {"root": "/srv"})
    assert resolved["m"] == {"src": "/srv/x.py"}


def test_resolve_vars_expands_through_a_list_of_dicts():
    """The shape autofix_bridge_modules uses — the nesting is two levels, not one."""
    resolved = resolve_vars(
        {"mods": [{"name": "a.py", "src": "{{ root }}/a.py"}]}, {"root": "/srv"}
    )
    assert resolved["mods"] == [{"name": "a.py", "src": "/srv/a.py"}]


def test_resolve_vars_leaves_a_brace_free_value_alone():
    """The accepting half: a brace-free value is left alone.

    Expansion must not rewrite values that held no template, and must not coerce non-strings. A
    recursive walk that stringified as it went would pass the three tests above and quietly turn
    every int and bool in the inventory into text.
    """
    values = {"ports": [80, 443], "on": True, "names": ["homelab"], "nested": {"n": 1}}
    assert resolve_vars(dict(values), {}) == values


# ── resolve_vars: a whole-value alias keeps the aliased variable's own type ────────────────────
#
# Ansible does this for real: a role default that is nothing but `"{{ other_var }}"` comes back
# as `other_var`'s own type, list included — confirmed against a live `ansible-playbook` run
# (see the commit that added this). `.render()` alone cannot reproduce that: Jinja templates
# always render to text, so a list-valued alias would come back as the literal characters of its
# Python repr, and a `{% for %}` loop over it would iterate one character at a time.


def test_resolve_vars_a_whole_value_alias_keeps_the_list_type():
    resolved = resolve_vars(
        {"node_cidrs": "{{ shared_cidrs }}"},
        {"shared_cidrs": ["10.42.0.1/32", "10.42.1.1/32"]},
    )
    assert resolved["node_cidrs"] == ["10.42.0.1/32", "10.42.1.1/32"]


def test_resolve_vars_a_partial_value_still_renders_to_a_string():
    """The rejecting half: text alongside the expression still goes through `.render()`.

    Only a value that IS the template, with nothing else in the string, gets the native-type
    treatment — the same boundary Ansible itself draws.
    """
    resolved = resolve_vars({"url": "https://{{ host }}/x"}, {"host": ["a", "b"]})
    assert resolved["url"] == "https://['a', 'b']/x"


# ── role defaults must not shadow the inventory ───────────────────────────────────────────────


def test_a_role_default_that_shadows_an_inventory_key_is_flagged():
    """The rejecting half.

    `{**base, **role_defaults(...)}` ranks role defaults ABOVE the group_vars and host_vars in
    `base`, which is the reverse of Ansible's own precedence — so a shared key renders a value no
    deploy would produce, while staying valid YAML and passing the schema check.
    """
    assert colliding_default_keys(
        {"crowdsec_k8s_image": "role-value", "own_key": 1},
        {"crowdsec_k8s_image": "inventory-value", "other": 2},
    ) == {"crowdsec_k8s_image"}


def test_a_role_default_with_its_own_key_space_is_clean():
    """The accepting half — a rule that flagged everything would pass the test above too."""
    assert (
        colliding_default_keys({"sonarr_port": 8989}, {"domain": "example.com"})
        == set()
    )
