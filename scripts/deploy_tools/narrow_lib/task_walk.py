#!/usr/bin/env python3
"""What one parsed task list says about tags: the files it pulls in and the tags that select it.

`setup_index.RoleIndex` reads a role's task files at a ref; this module holds the walks over
one already-parsed task list that it runs on each of them. They are pure functions of the
parsed YAML, so they read no git and no other file.

Run: uv run pytest scripts/deploy_tools/tests/test_narrow_setup_task_file_kinds.py
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/

import yaml

from lib import yaml_fast
from lib.narrow_git import CannotNarrow

# Ansible's two tags that do not select a subset. `--tags never` selects exactly the tasks
# opted out of every ordinary run, and `--tags always` selects the tasks that run whatever is
# asked for — so neither describes "the work this change needs". `docker_install`'s
# `tasks/install.yml` carries both `never` and `docker-engine-upgrade`, and printing that pair
# would tell an operator to run the engine upgrade a config edit never asked for (#2350).
#
# The two refuse differently. `never` beside a subject tag refuses the whole file. `always` is
# DROPPED from the answer instead (#3134), because an `always` task runs under every `--tags`
# value: the file's subject tags select its other tasks, and the `always` ones come along.
# `initial_setup/tasks/crons.yml` opens with two `always` tasks (its `DECIDED:` comment says
# why), and refusing on them made the role's most-edited task file narrow nothing. A file
# whose ONLY tag is `always` still refuses, since no tag is left to name its work.
_SPECIAL_TAGS = frozenset({"never", "always"})


# The spellings of a STATIC task import. `include_tasks` is absent here: a dynamic include's
# tasks run only when the include task itself is selected, and the tags on the included file's
# own tasks do not select the include — so a hit in such a file names a tag that may run
# nothing. `_dynamic_includes` handles the one include shape whose tags ARE knowable.
_STATIC_IMPORTS = (
    "import_tasks",
    "ansible.builtin.import_tasks",
    "ansible.legacy.import_tasks",
)


def _static_imports(tasks) -> list[str]:
    """The literal file names a task list imports statically, blocks walked through.

    A templated name (`{{ role_path }}/...`) is left out: which file it names is only known at
    run time, so a file reached only that way is not provably reachable.
    """
    out: list[str] = []
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        for key in ("block", "rescue", "always"):
            if isinstance(task.get(key), list):
                out += _static_imports(task[key])
        for key in _STATIC_IMPORTS:
            value = task.get(key)
            if isinstance(value, dict):
                value = value.get("file")
            if isinstance(value, str) and "{{" not in value:
                out.append(value)
    return out


_DYNAMIC_INCLUDES = (
    "include_tasks",
    "ansible.builtin.include_tasks",
    "ansible.legacy.include_tasks",
)


def _dynamic_includes(
    tasks, inherited: frozenset[str] = frozenset()
) -> list[tuple[str, frozenset[str] | str]]:
    """Each file a task list includes dynamically, with the tags that select all of it.

    An `include_tasks` runs only when its own tags match, and the included tasks then run on
    their own tags plus the include's `apply: {tags: ...}`. So a tag both carry selects the
    include AND every task it pulls in. `setup/k3s`'s weekly-shard file was that shape until
    H1.6, included in a loop under `[longhorn]` with `apply: {tags: [longhorn]}` (#4384).
    `import_tasks` takes no loop, so making it static was not an option.

    The second element is a refusal reason instead of a tag set when no tag provably does
    that: no `apply.tags`, an include with no tags of its own, the two disjoint, or a special
    tag on either side (`docker_install`'s engine upgrade is `never`-tagged on purpose).
    A templated file name is left out, as `_static_imports` leaves it out.
    """
    out: list[tuple[str, frozenset[str] | str]] = []
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        own = task.get("tags") or []
        own = [own] if isinstance(own, str) else own
        effective = inherited | frozenset(str(t) for t in own)
        for key in ("block", "rescue", "always"):
            if isinstance(task.get(key), list):
                out += _dynamic_includes(task[key], effective)
        for key in _DYNAMIC_INCLUDES:
            value = task.get(key)
            if value is None:
                continue
            spec = value if isinstance(value, dict) else {"file": value}
            name = spec.get("file")
            if not isinstance(name, str) or "{{" in name:
                continue
            applied = (spec.get("apply") or {}).get("tags") or []
            applied = frozenset(
                str(t) for t in ([applied] if isinstance(applied, str) else applied)
            )
            if (effective | applied) & _SPECIAL_TAGS:
                answer: frozenset[str] | str = (
                    f"{name} is included under a special tag, so no tag names its work"
                )
            elif not applied:
                answer = (
                    f"{name} is included without apply.tags, so its tasks keep only their "
                    "own tags and the include's tags may run none of them"
                )
            elif not effective & applied:
                answer = (
                    f"{name} is included under tags its apply.tags do not repeat, so no "
                    "one tag selects both the include and its tasks"
                )
            else:
                answer = effective & applied
            out.append((name, answer))
    return out


# The spellings of `set_fact`. Its mapping keys become host variables that survive the task
# file that set them, so a task file elsewhere in the role can read one under a different tag.
_SET_FACTS = (
    "set_fact",
    "ansible.builtin.set_fact",
    "ansible.legacy.set_fact",
)

# `set_fact`'s own option, not a fact it sets.
_SET_FACT_OPTIONS = frozenset({"cacheable"})


def _set_facts(tasks) -> set[str]:
    """Every variable name a task list derives with `set_fact`, blocks walked through."""
    out: set[str] = set()
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        for key in ("block", "rescue", "always"):
            if isinstance(task.get(key), list):
                out |= _set_facts(task[key])
        for key in _SET_FACTS:
            value = task.get(key)
            if isinstance(value, dict):
                out |= {str(k) for k in value} - _SET_FACT_OPTIONS
    return out


def _task_tags(tasks, inherited: frozenset[str]) -> list[frozenset[str]]:
    """The effective tag set of every task in a task list, blocks walked through.

    A `block`/`rescue`/`always` mapping is not a task: its own `tags:` apply to the tasks
    inside it, which is why the walk carries `inherited` down rather than counting the block
    itself. An empty frozenset in the result means a task no tag of this file selects.
    """
    out: list[frozenset[str]] = []
    for task in tasks or []:
        if not isinstance(task, dict):
            raise CannotNarrow("a task list holds something that is not a mapping")
        own = task.get("tags") or []
        own = [own] if isinstance(own, str) else own
        effective = inherited | frozenset(str(t) for t in own)
        nested = [
            task.get(key) for key in ("block", "rescue", "always") if task.get(key)
        ]
        if nested:
            for inner in nested:
                out += _task_tags(inner, effective)
        else:
            out.append(effective)
    return out


def file_tags(text: str) -> frozenset[str] | None:
    """The tags that select every task in one task file, or None when one is untagged.

    None is a refusal, and it is the common case for a file nobody tagged: `main.yml`'s
    imports, `unit-logging.yml`, `stale-checkout-check.yml`. Those inherit their tags from
    the import site, so a hit in one says nothing about which tag applies it.
    """
    try:
        doc = yaml_fast.safe_load(text)
    except yaml.YAMLError as exc:
        raise CannotNarrow(f"a task file does not parse: {exc}") from exc
    if not isinstance(doc, list):
        raise CannotNarrow("a task file is not a list of tasks")
    per_task = _task_tags(doc, frozenset())
    if not per_task or any(not tags for tags in per_task):
        return None
    return frozenset().union(*per_task)
