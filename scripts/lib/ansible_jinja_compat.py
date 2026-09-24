#!/usr/bin/env python3
"""Ansible's `bool` filter, reimplemented for a vanilla Jinja2 environment.

A render guard builds its own `jinja2.Environment`, which has no `bool`. A template written
for Ansible that uses `x | bool` therefore fails to render there with
`TemplateRuntimeError: No filter named 'bool' found`, even though production renders it fine.

This is the LIGHT tier, and it exists for exactly one caller: `lib.k8s_context`, whose own
`DECIDED:` marker gives the reason — importing `ansible.plugins.filter.core` costs ~190 ms and
`probe_lib/monitors.py` reaches that module. Every render guard takes the real filters from
`lib.ansible_jinja_env` instead. Faithfulness to Ansible matters more than convenience here —
see `ansible_bool` for the input shape that makes a naive stub agree with Ansible everywhere
except the cases the filter exists for.
"""

# `ansible.plugins.filter.core._valid_bool_true` / `_valid_bool_false`, copied rather than
# imported: importing `ansible.plugins.filter.core` costs ~190 ms and `probe_lib/monitors.py`
# reaches this module. `test_ansible_bool_agrees_with_ansible_core_to_bool` pins the copy.
BOOLEANS_TRUE = frozenset({"yes", "true", "1", "on"})
BOOLEANS_FALSE = frozenset({"no", "false", "0", "off"})


def ansible_bool(value) -> bool:
    """Mirror Ansible's `bool` Jinja filter, `ansible.plugins.filter.core.to_bool`.

    Vanilla Jinja2 has no `bool` filter, so a template guarding on `x | bool` renders here with
    `TemplateRuntimeError: No filter named 'bool' found`. Faithfulness matters more than
    convenience: the reason templates use `| bool` at all is that `-e var=false` arrives as the
    STRING "false", which plain Jinja truthiness reads as True. A stub that just called Python's
    bool() would agree with Ansible on real booleans and disagree on exactly the inputs the
    filter exists for, so the test would pass while production took the other branch.

    The filter is `to_bool`, NOT `module_utils.parsing.convert_bool.boolean` — that one serves
    module argument parsing and accepts `t`, `y`, `f`, `n` and any non-zero int. Until #2074
    the shim mirrored `boolean`, so `'t'`, `'y'`, `2`, `-1` and `' True '` rendered True under
    the guard and False in production. `to_bool` lowercases without stripping, stringifies
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
