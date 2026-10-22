"""What a playbook and a task list say about tags, without touching git.

`narrow_setup` asks two questions of the playbook its remediation prints. Which roles does it
apply — a role no play lists under `roles:` has no tag that reaches a host through it. And
which tags do the OTHER roles it applies declare — a tag two roles declare selects both, so
`initial_setup.yml --tags firewall` would apply a role the change never touched (#2350). And
which of the role's task files does a play reach by `include_role ... tasks_from:` rather than
through its `roles:` entry, under which tags (#5628).

It reads alone because nothing here reads the repository. Every function takes text a caller already fetched, which is also
why `narrow_setup.foreign_tags` — the one that walks a tree through `git show` — stays over
there beside the other git readers.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/

import yaml

from lib import yaml_fast


def playbook_roles(text: str | None) -> set[str]:
    """Every role name a playbook lists under some play's `roles:`.

    Both entry shapes: the inline `{ role: k3s, tags: [...] }` and the multi-line `- role:`
    form `nut_host` uses. A play with no `roles:` contributes nothing, which is how the
    `include_role` plays in `k3s-bringup.yml` are skipped — a role reached that way takes its
    tags from the including task, not from its own.

    A playbook that does not parse answers the empty set rather than raising. Every caller
    treats "not listed" as a refusal, so an unreadable playbook refuses through them.
    """
    if text is None:
        return set()
    try:
        plays = yaml_fast.safe_load(text)
    except yaml.YAMLError:
        return set()
    names: set[str] = set()
    for play in plays if isinstance(plays, list) else []:
        if not isinstance(play, dict):
            continue
        for entry in play.get("roles") or []:
            if isinstance(entry, dict):
                entry = entry.get("role") or entry.get("name")
            if isinstance(entry, str):
                names.add(entry)
    return names


def playbook_applies_role(text: str | None, role: str) -> bool:
    """Whether a playbook lists `role` in some play's `roles:`, the path its role tag takes."""
    return role in playbook_roles(text)


def declared_tags(tasks) -> set[str]:
    """Every tag name a parsed task list declares, blocks walked through.

    A block's own `tags:` counts: it is a tag an operator can select, whichever tasks it
    reaches. Used only to find a tag TWO roles declare, so it is deliberately wider than
    `task_walk._task_tags` — it asks "does this name exist over there", not "what
    does it select".
    """
    out: set[str] = set()
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        own = task.get("tags") or []
        own = [own] if isinstance(own, str) else own
        out |= {str(t) for t in own}
        for key in ("block", "rescue", "always"):
            if isinstance(task.get(key), list):
                out |= declared_tags(task[key])
    return out


# The spellings of a task that runs one task file of a role from a play.
_ROLE_INCLUDES = (
    "include_role",
    "ansible.builtin.include_role",
    "ansible.legacy.include_role",
    "import_role",
    "ansible.builtin.import_role",
    "ansible.legacy.import_role",
)


def play_role_includes(
    text: str | None, role: str
) -> list[tuple[str, frozenset[str] | str]]:
    """Each `tasks_from:` a play runs of `role`, with the tags on the task that runs it.

    `k3s-bringup.yml` runs `tasks/agent.yml` only from its join play, as an `include_role`
    tagged `k3s_agent`, so `--tags k3s_agent` with `-e join_agent=<host>` is the command that
    applies it. A file reached this way is not in `RoleIndex.reachable`, which reads only the
    role's `tasks/main`, and refused to narrow before #5628.

    The second element is a refusal reason instead of a tag set when the including task names
    no tag, carries a special one, or passes `apply:`, whose tags this does not follow. Plays'
    own `tags:` are inherited, as a block's are. A templated `tasks_from:` is left out, as
    `task_walk._static_imports` leaves out a templated import. Whether the file's OWN tasks
    carry the tag too is the caller's question: it holds the file.
    """
    if text is None:
        return []
    try:
        plays = yaml_fast.safe_load(text)
    except yaml.YAMLError:
        return []
    out: list[tuple[str, frozenset[str] | str]] = []
    for play in plays if isinstance(plays, list) else []:
        if not isinstance(play, dict):
            continue
        inherited = _tag_set(play.get("tags"))
        for section in ("pre_tasks", "tasks", "post_tasks"):
            out += _role_includes(play.get(section), role, inherited)
    return out


def _tag_set(value) -> frozenset[str]:
    """A `tags:` value as a set, whether written as one string or a list."""
    value = value or []
    return frozenset(str(t) for t in ([value] if isinstance(value, str) else value))


def _role_includes(
    tasks, role: str, inherited: frozenset[str]
) -> list[tuple[str, frozenset[str] | str]]:
    """`play_role_includes` over one task list, blocks walked through."""
    out: list[tuple[str, frozenset[str] | str]] = []
    for task in tasks if isinstance(tasks, list) else []:
        if not isinstance(task, dict):
            continue
        effective = inherited | _tag_set(task.get("tags"))
        for key in ("block", "rescue", "always"):
            out += _role_includes(task.get(key), role, effective)
        for key in _ROLE_INCLUDES:
            spec = task.get(key)
            if not isinstance(spec, dict) or spec.get("name") != role:
                continue
            name = spec.get("tasks_from")
            if not isinstance(name, str) or "{{" in name:
                continue
            answer: frozenset[str] | str = effective
            if "apply" in spec:
                answer = (
                    f"{name} is included with apply:, whose tags this does not follow"
                )
            elif not effective:
                answer = f"{name} is included by a play task that carries no tags"
            elif "never" in effective:
                answer = f"{name} is included under the special tag never"
            out.append((name, answer))
    return out
