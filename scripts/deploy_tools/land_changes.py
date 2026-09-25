#!/usr/bin/env python3
"""One reading of a changed-path list, for the five callers that need it.

`land_tags.plane_note`, `land_tags.self_applied`, `land_tags.self_applied_command`,
`land_reach.remaining_setup_hosts_note` and `deploy_tags._cmd_blockers` all ask the same
two questions of the same shape of list: what does it reach once the quiet paths are
dropped, and does any loud path sit under a bring-up playbook the deployer never applies.
Each of the first four carried its own `[p for p in files if p not in quiet]`, and two of
them their own `_BROAD_MANUAL_PREFIXES` test (#2419). Four copies of a filter is four
places for a later widening to land in three.

Its own module rather than a function in `land_tags`, for two reasons. `land_tags` imports
`land_reach`, so anything both read has to sit below both or the import cycles. And
`land_tags` is AT the 600-line cap, which is the same argument `narrow_paths.py` carries for
sitting beside `narrow_broad` rather than inside it.

`deploy_tags._cmd_blockers` reads through this too (#2541), which makes five callers. It
asks the same two questions of a range it reads itself, and the quiet set it passes is the
one this function's `quiet` parameter was always for — #2419's stated reason for excluding
it ("one function covering all six would take the filter as a parameter") was wrong, because
the filter is a parameter already.

`deploy_tags.changed` is the one site left out, and deliberately. It has no quiet set and
reads only `.changes`, so routing it here would compute a `manual` list nothing reads and
unwrap a three-field tuple for the one `services_from_changed_paths` call it already makes.
A comment at that site says the same.

Run: uv run pytest scripts/deploy_tools/tests/test_land_changes.py
"""

import sys
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.repo_paths import GITOPS_DEPLOY_FILES

sys.path.insert(0, str(GITOPS_DEPLOY_FILES))

from deploy_logic import (
    ChangeSet,
    _BROAD_MANUAL_PREFIXES,
    services_from_changed_paths,
)


class LoudChanges(NamedTuple):
    """What a landing's file list reaches once the quiet paths are dropped.

    Attributes:
      changes: the deployer's own `ChangeSet` over the loud paths — `.setup_roles`,
        `.broad_deploy` and `.k8s` are what the callers read off it.
      manual: the loud paths under `_BROAD_MANUAL_PREFIXES` — the bring-up playbooks, which
        run by hand by construction and park the tick outright.
      loud: the filtered path list itself. `deploy_tags._cmd_blockers` counts it in the
        line it prints when nothing blocks, and a caller that re-derived it would be the
        fifth copy of the filter this module exists to remove.
    """

    changes: ChangeSet
    manual: list[str]
    loud: list[str]


def changes_for(files, quiet=()) -> LoudChanges:
    """`files` minus `quiet`, read through the deployer's own mapper.

    Args:
      files: the landing's changed paths.
      quiet: the broad-plane paths whose diff carries no content change, from
        `deploy_tags.comment_only_paths`. A playbook named for three edited comments has
        nothing to apply, and PR #843 ended `needs-manual-apply` for exactly that (#848).
    """
    quiet = set(quiet)
    loud = [p for p in files if p not in quiet]
    return LoudChanges(
        services_from_changed_paths(loud),
        [p for p in loud if p.startswith(_BROAD_MANUAL_PREFIXES)],
        loud,
    )
