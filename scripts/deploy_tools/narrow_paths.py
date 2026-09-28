#!/usr/bin/env python3
"""The rules `narrow_broad` keeps outside itself: a path shape, and a role-directory read.

Split out for the reason `narrow_containers.py` was (#2044): `narrow_broad.py` sits at its
600-line cap, so a rule added there has to live beside it. It imports nothing from
`narrow_broad`, so the two cannot cycle — the role trees a caller reads are passed in rather
than imported back.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from collections.abc import Iterable
from pathlib import Path

from lib.git import git
from lib.narrow_git import CannotNarrow


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


def role_is_gone(role: str, ref: str, cwd: Path, trees: Iterable[str]) -> bool:
    """Whether `role`'s directory is absent from every one of `trees` at `ref`.

    A range that DELETES a role still lists every path the role owned as changed, so
    `narrow_broad._changed_half` handed `_role_tags` a role with no `containers_list` entry
    and no caller, which refuses — every role retirement, however small, cost the tick a
    full `ansible/deploy.yml` (#2879).

    A role directory that no longer exists at the new ref applies nothing: no play visits
    it, and the objects it owned are either orphaned, which the prune check reports, or
    re-owned by a role whose own paths changed in the same range and maps to its own tag.
    That is the reading `narrow_containers.entry_change_tags` already gives a REMOVED
    `containers_list` entry (#2046) — no `--tags` value undoes a removal, and neither does
    the whole play, so refusing bought no reconciliation and cost twenty minutes.

    The caller drops a gone role BEFORE `narrow_broad._role_tags` looks its callers up:
    that graph is walked from the WORKING TREE, which the deployer has at the OLD ref, so a
    deleted role still shows its callers there.

    Read with `git ls-tree`, which answers for a directory where `lib.narrow_git.show_at`
    answers only for a file. A git failure raises rather than reading as "gone": doubt runs
    the whole play.
    """
    paths = [f"{tree}/{role}" for tree in trees]
    r = git("ls-tree", "--name-only", ref, "--", *paths, cwd=cwd, check=False)
    if r.returncode:
        raise CannotNarrow(
            f"`git ls-tree` could not read {role} at {ref}: {r.stderr.strip()}"
        )
    return not r.stdout.strip()
