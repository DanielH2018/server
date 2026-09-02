#!/usr/bin/env python3
r"""No `cmd:` jsonpath filter loses its double quotes to the command module's shlex-split.

`ansible.builtin.command` shlex-splits `cmd` before exec'ing it. A jsonpath filter written
as `-o jsonpath={.items[0].status.conditions[?(@.type=="Ready")].status}` has its inner
double quotes stripped by that split, and kubectl rejects the result:

    unrecognized identifier Ready

rc != 0, empty stdout — which reads as "the resource isn't ready yet" rather than "the
command itself is malformed", so a `failed_when` built on it fails every time, not just when
the check is actually false. The fix is to single-quote the whole jsonpath argument, which
the shlex-split leaves intact (verified: single-quoted survives, bare-quoted does not — see
ansible/roles/setup/k3s/tasks/server.yml's comment for the same reasoning).

This trap has bitten this repo three times: ansible/roles/setup/k3s/tasks/server.yml and
ansible/roles/setup/k3s/tasks/agent_verify.yml, then ansible/roles/k8s/pihole/tasks/roll_one.yml.
Three recurrences is this repo's own threshold (CLAUDE.md, "Review & Memory Hygiene") for
turning a comment into an executable check.

A fourth then slipped past THIS FILE, because the check was one character class too narrow.
shlex strips backslashes as readily as it strips double quotes, and `\.` is how a jsonpath
escapes a dot that belongs to an annotation KEY. k8s/volume-claim's short-circuit shipped with
a bare one, read empty on every claim across two full deploys, and cost nothing but the saving
it was written to deliver. The double-quote form at least fails loudly; the backslash form
returns empty with rc 0 and looks exactly like "the annotation is not set".

So there are two checks below, one per character class. If a third form of shlex damage turns
up, add a third rather than widening one regex until nobody can read it.

Run: uv run pytest ansible/tests/k8s/test_jsonpath_quoting.py
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from _helpers import ANSIBLE


_SKIP_DIRS = {"collections"}  # vendored third-party, not ours to fix

# A `jsonpath=` argument that is NOT immediately single-quoted but contains a double quote
# somewhere before the next whitespace — i.e. the double-quoted, unquoted form that
# shlex-split corrupts. A jsonpath already wrapped in single quotes (`jsonpath='{...}'`)
# starts with a quote right after `=` and is excluded by the negative lookahead.
#
# Both safe spellings have to be excluded, because this repo uses both: `jsonpath='{...}'`
# (pihole/roll_one.yml, setup/k3s/agent_verify.yml) and `'jsonpath={...}'` (volume-revert's
# argv list). The lookbehind covers the second — without it the checks below flag correctly
# quoted code, which is how a guard gets weakened or deleted rather than obeyed.
_UNQUOTED_JSONPATH_WITH_DOUBLE_QUOTE = re.compile(r"(?<!')jsonpath=(?!')\S*\"")

# Same shlex mechanism, different character class, and a nastier failure. A `\.` escape marks a
# dot that belongs to a KEY rather than to jsonpath's field traversal — `{.metadata.annotations
# .homelab\.daniel-hunter\.com/seeded}`. shlex eats the backslashes, kubectl then looks for a
# nested map that does not exist, and returns EMPTY WITH rc 0. Nothing fails; the caller just
# silently reads "no annotation" forever.
_UNQUOTED_JSONPATH_WITH_BACKSLASH = re.compile(r"(?<!')jsonpath=(?!')\S*\\")


def _yaml_files() -> list[Path]:
    files = []
    for pattern in ("*.yml", "*.yaml"):
        for path in ANSIBLE.rglob(pattern):
            if _SKIP_DIRS.isdisjoint(path.parts):
                files.append(path)
    return files


def _iter_dicts(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _iter_dicts(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_dicts(item)


def _command_cmds() -> list[tuple[Path, str, str]]:
    """(file, task name, cmd string) for every command/shell task in ansible/."""
    found = []
    for path in _yaml_files():
        try:
            docs = list(yaml.safe_load_all(path.read_text()))
        except yaml.YAMLError:
            continue
        for doc in docs:
            for node in _iter_dicts(doc):
                if not isinstance(node, dict):
                    continue
                for module in ("ansible.builtin.command", "ansible.builtin.shell"):
                    value = node.get(module)
                    cmd = None
                    if isinstance(value, dict) and isinstance(value.get("cmd"), str):
                        cmd = value["cmd"]
                    elif isinstance(value, str):
                        cmd = value
                    if cmd is not None:
                        found.append(
                            (path, str(node.get("name", "<unnamed task>")), cmd)
                        )
    return found


def test_no_unquoted_double_quoted_jsonpath():
    offenders = [
        f"{path.relative_to(ANSIBLE.parent)}: task {name!r}"
        for path, name, cmd in _command_cmds()
        if _UNQUOTED_JSONPATH_WITH_DOUBLE_QUOTE.search(cmd)
    ]
    assert not offenders, (
        "these tasks' cmd: has a jsonpath filter using a bare double quote (e.g. "
        '[?(@.type=="Ready")]) that the command module\'s shlex-split will strip, corrupting '
        "the jsonpath and making kubectl fail with 'unrecognized identifier'. Wrap the whole "
        "jsonpath argument in single quotes instead — see "
        "ansible/roles/setup/k3s/tasks/server.yml for the pattern. Offenders:\n"
        + "\n".join(offenders)
    )


def test_no_unquoted_backslash_escaped_jsonpath():
    """The silent half of the same trap — no error, just an empty result forever.

    Caught in production rather than by this file: k8s/volume-claim's short-circuit read a
    `homelab.daniel-hunter.com/seeded` annotation through a bare `\\.`-escaped jsonpath, and
    read empty on all 25 claims across two full deploys while the annotation was present on
    every PVC. The whole optimisation was inert and every check stayed green.
    """
    offenders = [
        f"{path.relative_to(ANSIBLE.parent)}: task {name!r}"
        for path, name, cmd in _command_cmds()
        if _UNQUOTED_JSONPATH_WITH_BACKSLASH.search(cmd)
    ]
    assert not offenders, (
        "these tasks' cmd: has a jsonpath filter with a bare backslash escape (e.g. "
        "{.metadata.annotations.example\\.com/key}) that the command module's shlex-split "
        "will strip. Unlike the double-quote case this does NOT fail loudly — kubectl "
        "traverses a nested map that does not exist and returns empty with rc 0, so the "
        "caller reads a permanent 'not set'. Wrap the whole jsonpath argument in single "
        "quotes. Offenders:\n" + "\n".join(offenders)
    )
