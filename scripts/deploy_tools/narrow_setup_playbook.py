"""What a playbook and a task list say about tags, without touching git.

`narrow_setup` asks two questions of the playbook its remediation prints. Which roles does it
apply — a role no play lists under `roles:` has no tag that reaches a host through it. And
which tags do the OTHER roles it applies declare — a tag two roles declare selects both, so
`initial_setup.yml --tags firewall` would apply a role the change never touched (#2350).

Split out of `narrow_setup.py` for its module-length cap, and it is the natural half: nothing
here reads the repository. Every function takes text a caller already fetched, which is also
why `narrow_setup.foreign_tags` — the one that walks a tree through `git show` — stays over
there beside the other git readers.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

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
    `narrow_setup_index._task_tags` — it asks "does this name exist over there", not "what
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
