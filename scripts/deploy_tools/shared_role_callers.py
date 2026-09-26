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


# DECIDED: transitive, where `land_tags.covered_roles` is not. land.sh asks whether ONE
# landing deployed every caller, and a non-transitive answer only errs toward a note. Here
# the question is whether the fleet has been redeployed since, and a non-transitive answer
# would leave `longhorn-api` and `volume-revert` — whose only callers are shared — with a
# line nothing but a hand clears, which is the defect this exists to remove.
#
# DECIDED: a caller that never runs `manifests` is dropped rather than required. It writes
# no record, so requiring it keeps the line forever: `n8n-images` builds images through
# `image-builder`, renders no manifest, and has no record on daniel-box. Dropping it means
# an `image-builder` line can discharge while `n8n-images` alone is behind. That line is a
# banner entry that never pages, and a permanent one is the always-red surface #2570
# refused, so the gap is taken. A role left with no recording caller still keeps its line.
def recorded_callers(roles, ctx: Context) -> dict[str, list[str]]:
    """For each role, the declared tags that apply it AND write a release record."""
    writers = _tags(RECORD_WRITER, ctx)
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
