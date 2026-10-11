#!/usr/bin/env python3
"""Every name that carries a changed key of another role's vars file onward (#4361).

`narrow_setup.foreign_key_tags` asks the reading role about each changed key it names, and a
key it never names adds no tag. That skip holds only once every name whose value is built
from the key has been asked about too. `claude_code_agent_primary_profile` interpolates
thirteen other `claude_code` defaults, and `initial_setup` names it and none of those, so an
edit to `claude_code_agent_user_home` alone reached `initial_setup` through a word it never
wrote. Once the tick drops a role whose answer is empty, that miss is a change nothing
applies.

Three kinds of name carry a value onward, and the walk follows each to a fixed point:

  - another top-level key of the owner role's `defaults/` or `vars/`;
  - a top-level key of an inventory YAML file, such as daniel-box's
    `claude_code_user: "{{ claude_code_agent_user }}"`;
  - a fact the owner role's task files set with `set_fact`, which outlives the role.

`hosts.ini` can carry host variables inline, and this walk cannot read it as a mapping, so
a key it names refuses rather than being followed.

Run: uv run pytest scripts/deploy_tools/tests/test_narrow_setup_vars_walk.py
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/

import re

import yaml

from lib import yaml_fast
from lib.narrow_git import CannotNarrow, mapping_at, show_at
from deploy_tools.narrow_lib.setup_index import (
    _is_role_yaml,
    _scalars,
    _top_level_keys_naming,
    _tracked,
)
from deploy_tools.narrow_lib.task_walk import _SET_FACT_OPTIONS, _SET_FACTS

INVENTORY = "ansible/inventory/"

# The role directory a `defaults/` or `vars/` path sits in, trailing slash kept.
_OWNER = re.compile(r"^(.*?/)(?:defaults|vars)/")


def _fact_values(tasks) -> list[tuple[str, object]]:
    """Each fact a task list sets with `set_fact`, beside its value, blocks walked through."""
    out: list[tuple[str, object]] = []
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        for key in ("block", "rescue", "always"):
            if isinstance(task.get(key), list):
                out += _fact_values(task[key])
        for key in _SET_FACTS:
            value = task.get(key)
            if isinstance(value, dict):
                out += [
                    (str(k), v) for k, v in value.items() if k not in _SET_FACT_OPTIONS
                ]
    return out


def _carriers(owner: str, ref: str, repo: str):
    """The mappings, facts and unreadable inventory files the walk searches, read at `ref`."""
    docs: list[dict] = []
    facts: list[tuple[str, object]] = []
    opaque: dict[str, str] = {}
    for path in _tracked(ref, owner, repo):
        rel = path[len(owner) :]
        if not rel.startswith(("defaults/", "vars/", "tasks/")) or not _is_role_yaml(
            rel
        ):
            continue
        text = show_at(ref, path, repo) or ""
        if rel.startswith("tasks/"):
            try:
                doc = yaml_fast.safe_load(text)
            except yaml.YAMLError as exc:
                raise CannotNarrow(f"{path} does not parse: {exc}") from exc
            facts += _fact_values(doc if isinstance(doc, list) else [])
        else:
            docs.append(mapping_at(text, path))
    for path in _tracked(ref, INVENTORY, repo):
        text = show_at(ref, path, repo) or ""
        if path.endswith((".yml", ".yaml")):
            docs.append(mapping_at(text, path))
        else:
            opaque[path] = text
    return docs, facts, opaque


def keys_reaching(path: str, keys: set[str], ref: str, repo: str) -> set[str]:
    """`keys`, plus every name whose value is built from one of them, transitively.

    Args:
        path: the owner role's changed `defaults/` or `vars/` file.
        keys: the top-level keys of `path` whose value the range changed.
        ref: the commit to read the owner role and the inventory at.
        repo: the checkout to read.

    Raises:
        CannotNarrow: an inventory file that is not YAML names one of the names, or a file
            the walk reads does not parse.
    """
    match = _OWNER.match(path)
    if match is None:
        raise CannotNarrow(f"{path} is not under a role's defaults/ or vars/")
    docs, facts, opaque = _carriers(match.group(1), ref, repo)
    out = set(keys)
    todo = sorted(keys)
    while todo:
        name = todo.pop()
        word = re.compile(rf"(?<!\w){re.escape(name)}(?!\w)")
        for other, text in opaque.items():
            if word.search(text):
                raise CannotNarrow(
                    f"{other} names {name}, and it is not a YAML mapping this walk can follow"
                )
        found: set[str] = set().union(*(_top_level_keys_naming(d, word) for d in docs))
        found |= {
            fact
            for fact, value in facts
            if any(word.search(s) for s in _scalars(value, set()))
        }
        for other in sorted(found - out):
            out.add(other)
            todo.append(other)
    return out
