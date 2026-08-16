#!/usr/bin/env python3
"""No `cmd:` jsonpath filter loses its double quotes to the command module's shlex-split.

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

Run: uv run pytest ansible/tests/test_jsonpath_quoting.py
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ANSIBLE = Path(__file__).resolve().parents[1]
_SKIP_DIRS = {"collections"}  # vendored third-party, not ours to fix

# A `jsonpath=` argument that is NOT immediately single-quoted but contains a double quote
# somewhere before the next whitespace — i.e. the double-quoted, unquoted form that
# shlex-split corrupts. A jsonpath already wrapped in single quotes (`jsonpath='{...}'`)
# starts with a quote right after `=` and is excluded by the negative lookahead.
_UNQUOTED_JSONPATH_WITH_DOUBLE_QUOTE = re.compile(r"jsonpath=(?!')\S*\"")


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
