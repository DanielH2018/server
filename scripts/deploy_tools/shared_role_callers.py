#!/usr/bin/env python3
"""Which deploy tags' release records prove a shared k8s role's change was applied.

THE PROBLEM. `deploy_defer.discharge_k8s_unapplied` drops a `k8s_unapplied` line when the
service's release record names a commit carrying the change. A shared role — `manifests`,
`image-builder`, `game-stats-lib` — has no `containers_list` entry, so it never gets a record
of its own, and its line stood until somebody ran `gitops_state.py clear-k8s-unapplied` by
hand (#2643). A full deploy on 2026-09-26 applied every caller of all three and left all three
lines in place.

WHAT THIS DERIVES. For each role, the declared tags whose deploy runs it, followed through a
caller that is itself shared (`narrow_broad._role_tags`, the expansion `Release Staleness
Drift` and the deploy-plane narrowing already use), then kept only where that tag's deploy
also runs `manifests`, whose `tasks/release_stamp.yml` writes the record.

WHO CALLS IT. The deployer, as a SUBPROCESS through `deploy_narrow.shared_role_callers`,
because this parses YAML and the unit runs under `uv run --no-project` — the boundary
`narrow_setup.py` sits behind for the same reason. It prints one JSON object, role to sorted
tag list. An empty list means no record can ever prove the role applied, and the deployer
keeps that line.

`caller_tags` and `expand_shared_tags` ask the question a CHANGE asks: which tags deploy a
shared role at all, record or no record. `deploy_run` reads them to turn `deploy.sh --tags
volume-snapshot` into a deploy of every caller, and `land_tags` reads them to deploy a shared
role's callers instead of reporting `needs-manual-apply` (#2704).

Usage: shared_role_callers.py [--repo PATH] ROLE [ROLE ...]
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import argparse
import json

from deploy_tools.narrow_broad import CannotNarrow, Context, _role_tags, context_for

# The shared role whose tasks write every k8s release record.
RECORD_WRITER = "manifests"


def _tags(role: str, ctx: Context) -> set[str]:
    """`_role_tags` for one role, with a role no tag reaches read as reaching nothing."""
    try:
        return _role_tags({role}, ctx)
    except CannotNarrow:
        return set()


def caller_tags(
    role: str, declared: set[str], callers: dict[str, set[str]]
) -> set[str]:
    """The declared tags whose deploy runs `role`, followed through shared callers.

    `_role_tags` for one role, except that a caller with neither an entry nor a caller of its
    own contributes nothing rather than refusing the whole answer. Nothing can deploy that
    caller, so there is nothing more to run for it. An empty set means no tag deploys `role`.

    Args:
        role: a role directory under `roles/k8s/`.
        declared: every name `containers_list` declares as a tag.
        callers: the role-caller graph, as `lib.k8s_roles.role_callers` walks it.
    """
    tags: set[str] = set()
    pending, seen = {role}, set()
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        if name in declared:
            tags.add(name)
        else:
            pending |= callers.get(name, set())
    return tags


def expand_shared_tags(
    tags: list[str], declared: set[str], callers: dict[str, set[str]]
) -> tuple[list[str], dict[str, list[str]]]:
    """`tags` with each shared-role name replaced by the tags of every role that runs it.

    A shared role runs with its caller's variables (`manifests_service`, the claims, the
    rollout names), so it has nothing to act on without a caller. Its callers are therefore
    the unit a `--tags` run can select.

    Args:
        tags: the tags as typed, in order.
        declared: every name `containers_list` declares as a tag.
        callers: the role-caller graph, as `lib.k8s_roles.role_callers` walks it.

    Returns:
        The expanded list, in typed order and de-duplicated, and for each name that was
        replaced the sorted tags it became. A name that is neither declared nor run by any
        declared role stays as typed, so `deploy_tags.validate` refuses it by name.
    """
    out: list[str] = []
    replaced: dict[str, list[str]] = {}
    for tag in tags:
        reached = [] if tag in declared else sorted(caller_tags(tag, declared, callers))
        if reached:
            replaced[tag] = reached
        for name in reached or [tag]:
            if name not in out:
                out.append(name)
    return out, replaced


# DECIDED: transitive, as `caller_tags` is. The land-side check that stopped at the first
# hop (`covered_roles`) was replaced by `caller_tags` in #2704. Here the question is whether
# the fleet has been redeployed since, and a non-transitive answer
# would leave `longhorn-api` and `volume-revert` — whose only callers are shared — with a
# line nothing but a hand clears, which is the defect this exists to remove.
# DECIDED: a caller that never runs `manifests` is dropped rather than required. It writes
# no record, so requiring it keeps the line forever. `n8n-images` was the one live instance
# until #2813 folded it into n8n: of the declared entries, its role was the only one that
# rendered no manifest, so `image-builder`'s set dropped it. That means an `image-builder` line can
# discharge while `n8n-images` alone is behind. That line is a banner entry that never
# pages, and a permanent one is the always-red surface #2570 refused, so the gap is taken. A
# role left with no recording caller at all still keeps its line.
#
# `grep -rL k8s/manifests ansible/roles/k8s/*/tasks/main.yml` against the declared entries is
# how to re-derive whether a second instance has appeared.
#
# An entry declaring a SECOND tag was the other way a recording caller hid from `_role_tags`,
# which stops at a role with an entry and never reads that entry's other tags: `n8n-images`
# declared `tags: [n8n-images, n8n]`, so a `--tags n8n` deploy ran the builder and wrote
# `n8n.json` while the derivation saw nothing (#2666). `_co_applied` widened the answer to
# cover it. #2813 folded n8n-images into n8n, no entry has declared a second tag since, and
# #2876 deleted the widening; restore it from git history if one does.
def _writer_tags(ctx: Context) -> set[str]:
    """The tags whose deploy runs `manifests`, and so writes a release record.

    Expanded caller by caller rather than in one `_tags(RECORD_WRITER)` call. A caller no tag
    applies — a role committed ahead of its `containers_list` entry, or read from a tree ahead
    of `ctx.ref` — applies nothing, and must not empty every other caller's tags with it. In
    one call it raised `CannotNarrow` for the whole set, and every shared role read as having
    no recording caller at all (#2813).
    """
    return {
        tag
        for caller in ctx.callers.get(RECORD_WRITER) or ()
        for tag in _tags(caller, ctx)
    }


def recorded_callers(roles, ctx: Context) -> dict[str, list[str]]:
    """For each role, the declared tags that apply it AND write a release record.

    `discharge_k8s_unapplied` requires a record from EVERY tag here, so a wider answer only
    ever makes a line harder to drop.
    """
    writers = _writer_tags(ctx)
    return {role: sorted(_tags(role, ctx) & writers) for role in roles}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("roles", nargs="+", help="role directories under roles/k8s/")
    parser.add_argument(
        "--repo", default=".", help="the checkout to read (default: the cwd)"
    )
    args = parser.parse_args(argv)
    ctx = context_for("HEAD", args.repo)
    print(json.dumps(recorded_callers(args.roles, ctx), sort_keys=True))
    return 0


if __name__ == "__main__":
    _sys.exit(main())
