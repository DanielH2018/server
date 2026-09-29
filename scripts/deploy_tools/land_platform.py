#!/usr/bin/env python3
"""Which of a PR's derived tags its own changed paths PROVE are a k3s change (#2730).

One tag name can select two services on two platforms: `wg-easy` is a `platform: k8s` entry on
daniel-box and a Compose entry on daniel-pi. `render_guard.hosts_for_tags` takes a `k8s_only`
subset that routes such a tag to the k8s entry alone, and #2718 filled it with the tags the k8s
role-caller graph named. A tag a changed PATH named was left out, so a PR touching only
`ansible/roles/k8s/wg-easy/templates/` still ran `deploy.sh -e target=daniel-pi --tags wg-easy`
— one extra ssh deploy of a Compose service the change never touched, recreating nothing.

THE PROOF IS THE TREE THE PATH SITS IN, and it has to be EVERY path that names the tag. A PR
touching both `roles/k8s/wg-easy/` and `roles/containers/wg-easy/` changes both services, so
that tag keeps routing to both hosts. Narrowing host routing is the dangerous direction --
issue #929 was a tag that reached no host while land.sh read `settled` and the Pi ran the old
container -- so this answers only for a tag whose whole path set sits under one tree.

A TAG NO PATH NAMES IS NEVER IN HERE, because this reads paths rather than `derived_tags`'
output. Nothing proves which platform such a tag belongs to, so it keeps reaching every host
that declares it.

Its own module beside `land_tags`, for the reason `land_changes.py` gives for sitting there:
`land_tags` is AT the 600-line cap. The import goes one way -- this reads `land_tags.tag_for`
and `land_tags` reads nothing here -- so no cycle.

Run: uv run pytest scripts/deploy_tools/tests/test_land_platform.py
"""

import sys
from pathlib import Path

# This module's own directory, because `sys.path[0]` is the ENTRY script's directory rather
# than this one: under the land.py shim that is scripts/deploy_tools, but under any other
# invoker it is not, and `land_tags` sits beside this file.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import land_tags

# The role tree a k3s workload's files sit in. `land_tags._K8S` anchors its own match to this
# same prefix, and `test_land_platform.py` holds the two against each other: a tree that moved
# under one spelling and not the other would leave this proving nothing while still returning
# tags.
K8S_TREE = "ansible/roles/k8s/"


def k8s_only_tags(files, declared: set[str] | None = None) -> list[str]:
    """The tags `files` derives whose every naming path sits under `K8S_TREE`.

    `files` is the PR's own changed-path list. `declared` is the `containers_list` tags read at
    the merge commit; None reads this checkout, as every `land_tags` mapper does.

    A path that names no tag is absent rather than restricting anything: `tag_for` is the one
    mapper for "which tag does this path name", so a `.md`, a role's own `tests/` and an
    unregistered role drop out here exactly as they drop out of the derivation.
    """
    declared = land_tags.declared_tags() if declared is None else declared
    trees: dict[str, set[bool]] = {}
    for path in files:
        if tag := land_tags.tag_for(path, declared):
            trees.setdefault(tag, set()).add(path.startswith(K8S_TREE))
    return sorted(tag for tag, in_k8s in trees.items() if in_k8s == {True})


def diff_range(ref: str) -> str:
    """The git range `deploy_tags.py changed <ref>` derives its tags from.

    Three dots, and HEAD on the right: `changed` reads `<ref>...HEAD` in the primary checkout
    (`deploy_tags._git_diff_paths`). The landing's fallback derivation reads the same range's
    paths to prove a tag's platform (#2738), so the two spellings have to be one spelling --
    a two-dot range would prove the platform of a different file set than the tags came from.
    `test_land_platform.py` holds this against that helper.
    """
    return f"{ref}...HEAD"
