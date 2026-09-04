#!/usr/bin/env python3
"""Ansible's `search` test and `bool` filter, reimplemented for a vanilla Jinja2 environment.

A render guard builds its own `jinja2.Environment`, which has neither of these. A template
written for Ansible that uses `x | bool` or `list | reject('search', pattern)` therefore fails
to render under the guard with `TemplateRuntimeError`, even though production renders it fine.
Registering these two restores the pair the templates in this repo actually reach for.

Faithfulness to Ansible matters more than convenience here — see `ansible_bool` for the input
shape that makes a naive stub agree with Ansible everywhere except the cases the filter exists
for. `scripts/lib/shell_lint.py` registers both on the environment it builds.
"""

import re


def ansible_search(value, pattern, ignorecase=False, multiline=False) -> bool:
    """Mirror Ansible's `search` Jinja test — a plain regex search, not a full match.

    Vanilla Jinja2 has no `search` test, so any template using Ansible's `search` (e.g.
    `list | reject('search', pattern)`) would otherwise fail to render here with
    `TemplateRuntimeError: No test named 'search'`. No current template needs it
    (docker-user-rules.sh.j2, the last one that did, retired at E7 2026-08-13) — kept
    registered so the next one that does just works.
    """
    flags = (re.I if ignorecase else 0) | (re.M if multiline else 0)
    return bool(re.search(pattern, str(value), flags))


BOOLEANS_TRUE = {"y", "yes", "on", "1", "true", "t"}
BOOLEANS_FALSE = {"n", "no", "off", "0", "false", "f", ""}


def ansible_bool(value) -> bool:
    """Mirror Ansible's `bool` filter (module_utils.parsing.convert_bool.boolean, strict=False).

    Vanilla Jinja2 has no `bool` filter, so a template guarding on `x | bool` renders here with
    `TemplateRuntimeError: No filter named 'bool' found`. Faithfulness matters more than
    convenience: the reason templates use `| bool` at all is that `-e var=false` arrives as the
    STRING "false", which plain Jinja truthiness reads as True. A stub that just called Python's
    bool() would agree with Ansible on real booleans and disagree on exactly the inputs the
    filter exists for, so the test would pass while production took the other branch.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    normalised = str(value).strip().lower()
    if normalised in BOOLEANS_TRUE:
        return True
    if normalised in BOOLEANS_FALSE:
        return False
    return False
