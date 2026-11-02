#!/usr/bin/env python3
"""Which of a PR's derived tags the landing may route to a `platform: k8s` entry alone.

`render_guard.hosts_for_tags` takes a `k8s_only` subset that routes a tag to its k8s entry
alone, and `classify` fills it from the PR's changed paths through this module. The subset
existed for a tag declared on two platforms: `wg-easy` was a k8s entry on daniel-box and a
Compose entry in `roles/containers/` on daniel-pi.

NO TAG IS DECLARED ON TWO PLATFORMS. The Pi's services are `compose_services` entries, its
`containers_list` is empty, and `reach.tag_for` names a tag only for a path under
`roles/k8s/`. So every tag a path derives is a k3s tag, and this returns them all. A tag no
path names is still absent, because this reads paths rather than `Reach.tags`' output.
`test_land_platform.py` fails if a tag returns to two platforms, which is the premise.

Its own module because `diff_range` restates the range `deploy_tags._git_diff_paths` reads,
and a test compares the two. That copy is of `deploy_lib/tags.py`, not of `land_tags`, so
folding this module into `land_tags` would not remove it.

Run: uv run pytest scripts/deploy_tools/tests/test_land_platform.py
"""

import sys
from pathlib import Path

# scripts/ for `land_tags`, and scripts/deploy_tools for `reach`: `sys.path[0]` is the ENTRY
# script's directory rather than this one, so no invoker can be relied on to supply either.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # scripts/
sys.path.insert(1, str(Path(__file__).resolve().parents[1]))  # scripts/deploy_tools

import reach
from deploy_tools.land_lib import land_tags


def k8s_only_tags(files, declared: set[str] | None = None) -> list[str]:
    """Every tag `files` derives, sorted.

    `files` is the PR's own changed-path list. `declared` is the `containers_list` tags read at
    the merge commit; None reads this checkout, as every `land_tags` mapper does.

    A path that names no tag is absent rather than restricting anything: `tag_for` is the one
    mapper for "which tag does this path name", so a `.md`, a role's own `tests/` and an
    unregistered role drop out here exactly as they drop out of the derivation.
    """
    declared = land_tags.declared_tags() if declared is None else declared
    return sorted({tag for path in files if (tag := reach.tag_for(path, declared))})


def diff_range(ref: str) -> str:
    """The git range `deploy_tags.py changed <ref>` derives its tags from.

    Three dots, and HEAD on the right: `changed` reads `<ref>...HEAD` in the primary checkout
    (`deploy_tags._git_diff_paths`). The landing's fallback derivation reads the same range's
    paths to name its tags, so the two spellings have to be one spelling -- a two-dot range
    would name the tags of a different file set than the tags came from.
    `test_land_platform.py` holds this against that helper.
    """
    return f"{ref}...HEAD"
