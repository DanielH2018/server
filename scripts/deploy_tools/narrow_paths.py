#!/usr/bin/env python3
"""The rules `narrow_broad` keeps outside itself: a path shape, and a role-directory read.

Split out for the reason `narrow_containers.py` was: `narrow_broad.py` sits at its
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
from lib.repo_paths import GITOPS_DEPLOY_FILES

# The deployer's own `files/`, for the one rule this module shares with the tick itself. Reached
# as a path entry rather than a copy, the way `narrow_broad` reaches `deploy_logic`; the import
# stays inside `is_prose`, because only the path entry is free at module import.
_sys.path.insert(0, str(GITOPS_DEPLOY_FILES))


def is_prose(path: str) -> bool:
    """Whether a changed path is documentation no playbook applies.

    `narrow_broad.PLAY_PREFIXES` matches by DIRECTORY, so `roles/containers/common/CLAUDE.md`
    refused under "is read by every deploy" and widened the tick to a full `ansible/deploy.yml`
    — 14 minutes for prose (#2448). `land_tags.role_for` drops a `.md` for the same reason,
    and so does `narrow_broad._sort_hits` for a grep hit.

    The rule itself is the deployer's `deploy_changes.is_doc`, reached through the index, so the
    tick and this derivation cannot disagree about what a `.md` is (#2810). It used to carve out
    a `.md` under `files/` or `templates/` as shippable; the `DECIDED:` on `is_doc` says why that
    carve-out went, and which guard replaced it. `narrow_setup._reaches_no_host` calls this too.
    """
    from deploy_logic import is_doc

    return is_doc(path)


def role_is_gone(role: str, ref: str, cwd: Path, trees: Iterable[str]) -> bool:
    """Whether `role`'s directory is absent from every one of `trees` at `ref`.

    A range that DELETES a role still lists every path the role owned as changed, so
    `narrow_broad._changed_half` handed `_role_tags` a role with no `containers_list` entry
    and no caller, which refuses — every role retirement, however small, cost the tick a
    full `ansible/deploy.yml`.

    A role directory that no longer exists at the new ref applies nothing: no play visits
    it, and the objects it owned are either orphaned, which the prune check reports, or
    re-owned by a role whose own paths changed in the same range and maps to its own tag.
    That is the reading `narrow_containers.entry_change_tags` already gives a REMOVED
    `containers_list` entry — no `--tags` value undoes a removal, and neither does
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
