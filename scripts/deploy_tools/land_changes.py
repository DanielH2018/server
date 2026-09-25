#!/usr/bin/env python3
"""One reading of a landing's changed-path list, for the four callers that need it.

`land_tags.plane_note`, `land_tags.self_applied`, `land_tags.self_applied_command` and
`land_reach.remaining_setup_hosts_note` all ask the same two questions of the same list: what
does it reach once the quiet paths are dropped, and does any loud path sit under a bring-up
playbook the deployer never applies. Each carried its own `[p for p in files if p not in
quiet]`, and two of them their own `_BROAD_MANUAL_PREFIXES` test (#2419). Four copies of a
filter is four places for a later widening to land in three.

Its own module rather than a function in `land_tags`, for two reasons. `land_tags` imports
`land_reach`, so anything both read has to sit below both or the import cycles. And
`land_tags` is AT the 600-line cap, which is the same argument `narrow_paths.py` carries for
sitting beside `narrow_broad` rather than inside it.

`deploy_tags.py`'s two sites are deliberately not folded in. `_cmd_blockers` filters with
`comment_only_paths` over a range it reads itself, and `changed` has no quiet set at all, so
one function covering all six would take the filter as a parameter and stop being one answer.

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
    """

    changes: ChangeSet
    manual: list[str]


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
    )
