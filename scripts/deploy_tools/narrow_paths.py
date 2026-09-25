#!/usr/bin/env python3
"""The path-shape rules `narrow_broad` applies before it reads anything from git.

Split out for the reason `narrow_containers.py` was (#2044): `narrow_broad.py` sits at its
600-line cap, so a rule added there has to live beside it. Pure over a path string, so it
imports nothing from `narrow_broad` and the two cannot cycle.
"""


def is_prose(path: str) -> bool:
    """Whether a changed broad-plane path is documentation no playbook applies.

    `narrow_broad.PLAY_PREFIXES` matches by DIRECTORY, so `roles/containers/common/CLAUDE.md`
    refused under "is read by every deploy" and widened the tick to a full `ansible/deploy.yml`
    -- 14 minutes for prose (#2448). `land_tags.role_for` drops a `.md` for the same reason,
    and so does `narrow_broad._sort_hits` for a grep hit.

    A `.md` under `files/` or `templates/` is NOT prose: a task can copy or render it onto a
    host. That carve-out is `narrow_setup._reaches_no_host`'s, restated because the two walk
    different trees.
    """
    if not path.endswith(".md"):
        return False
    return not {"files", "templates"} & set(path.split("/")[:-1])
