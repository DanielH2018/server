#!/usr/bin/env python3
"""Resolve which source files a `release_bin.yml` group deploys.

Shared because two checks need the same answer and must not be able to disagree about it:
`scripts/validate/validate_shell_templates.py` derives the cron rules that apply to a versioned
script, and `ansible/tests/test_release_bin_groups_have_no_secrets.py` refuses a group holding a
script that renders a credential inline. A group they resolve differently is a group one of them
silently stops covering — the same failure the shared template list inside release_bin.yml was
introduced to prevent.

THE SHAPE THAT MADE A SECOND RESOLVER WRONG. A caller hands release_bin.yml a GROUP NAME, and
the file list lives in that role's `defaults/main.yml`:

    ansible.builtin.import_tasks: "{{ role_path }}/../common/tasks/release_bin.yml"
    vars:
      release_bin_group: k3s-backup-health
      release_bin_templates: >-
        {{ (k3s_render_stamp_groups | selectattr('name', 'eq', 'k3s-backup-health')
            | first).templates }}

`release_bin_templates` therefore parses as a folded Jinja STRING, not a list. A resolver that
iterates it and keeps the dict entries finds nothing and reports a clean result — which is
exactly how the secrets guard shipped on 2026-08-29 scanning zero files while passing. Resolve
the group from the defaults instead; the literal-list form is still accepted because a caller may
pass one directly.
"""

from pathlib import Path

import yaml


def group_sources(group: str, task_file: Path):
    """Yield repo-relative srcs a group declares, from its role's defaults/main.yml.

    Structural rather than hardcoding `k3s_render_stamp_groups`: any list of dicts carrying
    `name` and `templates` defines a group, so a second role adopting the mechanism needs no
    edit here. `files` entries are dicts of {src, name} and are yielded by their src.
    """
    defaults = task_file.parent.parent / "defaults" / "main.yml"
    if not defaults.is_file():
        return
    try:
        doc = yaml.safe_load(defaults.read_text()) or {}
    except yaml.YAMLError:
        return
    for value in (doc or {}).values():
        if not isinstance(value, list):
            continue
        for entry in value:
            if not isinstance(entry, dict) or entry.get("name") != group:
                continue
            for src in entry.get("templates") or []:
                yield str(src)
            for item in entry.get("files") or []:
                if isinstance(item, dict) and item.get("src"):
                    yield str(item["src"])


def task_sources(task: dict, task_file: Path):
    """Yield every repo-relative src a single release_bin.yml import deploys.

    Accepts all three ways a caller can name its files: the group indirection above, a literal
    list of `.j2` paths in `release_bin_templates`, and `release_bin_files` dicts. The literal
    forms are checked first so a caller passing both is not silently reduced to one.
    """
    target = str(task.get("ansible.builtin.import_tasks") or "")
    if not target.endswith("release_bin.yml"):
        return
    task_vars = task.get("vars") or {}

    templates = task_vars.get("release_bin_templates")
    if isinstance(templates, list):
        for src in templates:
            if isinstance(src, str):
                yield src

    for item in task_vars.get("release_bin_files") or []:
        if isinstance(item, dict) and item.get("src"):
            yield str(item["src"])

    group = task_vars.get("release_bin_group")
    if group:
        yield from group_sources(str(group), task_file)


def _walk(node):
    """Yield every dict nested anywhere in a parsed task file."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def iter_sources(roles: Path):
    """Yield (task_file, src) for every release_bin.yml import under `roles`."""
    for path in sorted(roles.glob("**/tasks/*.yml")):
        if "/archive/" in str(path):
            continue
        try:
            doc = yaml.safe_load(path.read_text())
        except yaml.YAMLError:
            continue
        for task in _walk(doc):
            for src in task_sources(task, path):
                yield path, src


def host_name(src: str) -> str:
    """The name a rendered script takes on the host: basename minus `.j2`.

    release_bin.yml's own derivation. A `files` entry can rename (live_drift_check.py is
    deployed as live-drift-check.py), so this is only correct for templates.
    """
    return Path(src).name.removesuffix(".j2")
