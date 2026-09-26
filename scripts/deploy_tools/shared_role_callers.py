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
Drift` and the deploy-plane narrowing already use), widened by `_co_applied` to the entries
`_role_tags` cannot see, then kept only where that tag's deploy also runs `manifests`, whose
`tasks/release_stamp.yml` writes the record.

`_co_applied` is this module's own widening and stays here: the deploy-plane narrowing and
the release-staleness census read `_role_tags` too, and they ask which tags a CHANGE must be
deployed under, where this asks which records prove a deploy already happened.

WHO CALLS IT. The deployer, as a SUBPROCESS through `deploy_narrow.shared_role_callers`,
because this parses YAML and the unit runs under `uv run --no-project` — the boundary
`narrow_setup.py` sits behind for the same reason. It prints one JSON object, role to sorted
tag list. An empty list means no record can ever prove the role applied, and the deployer
keeps that line.

Usage: shared_role_callers.py [--repo PATH] ROLE [ROLE ...]
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import argparse
import json

from deploy_tools.narrow_broad import CannotNarrow, Context, _role_tags, context_for
from lib.render_guard import entry_tags_at

# The shared role whose tasks write every k8s release record.
RECORD_WRITER = "manifests"


def _tags(role: str, ctx: Context) -> set[str]:
    """`_role_tags` for one role, with a role no tag reaches read as reaching nothing."""
    try:
        return _role_tags({role}, ctx)
    except CannotNarrow:
        return set()


def _co_applied(role: str, entry_tags: dict[str, set[str]]) -> set[str]:
    """Every declared role a deploy of `role` also runs, `role` itself included.

    `_role_tags` stops at a role that has a `containers_list` entry, because that entry's
    NAME is a tag. It never reads the entry's other declared tags, and `n8n-images` declares
    `tags: [n8n-images, n8n]` — so a `--tags n8n` deploy runs the builder, writes `n8n.json`,
    and the derivation could not see that recording caller (#2666).

    The subset is what makes a record stand in. Role `S` is applied by every tag its own
    entry declares, so a record for `S` proves `role` ran only where EVERY tag that selects
    `S` also selects `role` — `n8n`'s `{n8n}` inside `n8n-images`'s `{n8n-images, n8n}`. A
    merely overlapping entry would let a deploy under its other tag write a record while
    `role` stayed behind.

    Known edge, accepted: a hand `--tags n8n --skip-tags n8n-images` run writes `n8n.json`
    without building, and discharges the line falsely. `--skip-tags` appears in no cron, no
    playbook and no skill here; the line it would drop is a banner entry that never pages.
    """
    mine = entry_tags.get(role)
    return {s for s, tags in entry_tags.items() if tags <= mine} if mine else set()


# DECIDED: transitive, where `land_tags.covered_roles` is not. land.sh asks whether ONE
# landing deployed every caller, and a non-transitive answer only errs toward a note. Here
# the question is whether the fleet has been redeployed since, and a non-transitive answer
# would leave `longhorn-api` and `volume-revert` — whose only callers are shared — with a
# line nothing but a hand clears, which is the defect this exists to remove.
# DECIDED: a caller that never runs `manifests` is dropped rather than required. It writes
# no record, so requiring it keeps the line forever: `longhorn-api` and `volume-revert` are
# reached only through shared roles, and `image-builder` is applied by builder entries that
# render no manifest. Dropping those means an `image-builder` line can discharge while one
# builder entry alone is behind. That line is a banner entry that never pages, and a
# permanent one is the always-red surface #2570 refused, so the gap is taken. A role left
# with no recording caller still keeps its line.
#
# `n8n-images` was this marker's worked example until #2666, and is no longer one: it has a
# recording caller, reached through `_co_applied` rather than through the caller graph.
def recorded_callers(
    roles, ctx: Context, entry_tags: dict[str, set[str]]
) -> dict[str, list[str]]:
    """For each role, the declared tags that apply it AND write a release record.

    `entry_tags` is name -> declared tags, as `render_guard.entry_tags_at` reads it at
    `ctx.ref`. Required rather than defaulted: a caller that forgot it would silently get
    the #2666 answer back. `discharge_k8s_unapplied` requires a record from EVERY tag here,
    so a union only ever makes a line harder to drop.
    """
    writers = _tags(RECORD_WRITER, ctx)
    out = {}
    for role in roles:
        tags = _tags(role, ctx)
        tags |= {alias for tag in tags for alias in _co_applied(tag, entry_tags)}
        out[role] = sorted(tags & writers)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("roles", nargs="+", help="role directories under roles/k8s/")
    parser.add_argument(
        "--repo", default=".", help="the checkout to read (default: the cwd)"
    )
    args = parser.parse_args(argv)
    ctx = context_for("HEAD", args.repo)
    # `ctx.ref` and `ctx.cwd`, not a fresh "HEAD" read: two reads at different refs can
    # disagree with `ctx.declared`, which `context_for` already filled in at that ref.
    entry_tags = entry_tags_at(ctx.ref, ctx.cwd)
    print(json.dumps(recorded_callers(args.roles, ctx, entry_tags), sort_keys=True))
    return 0


if __name__ == "__main__":
    _sys.exit(main())
