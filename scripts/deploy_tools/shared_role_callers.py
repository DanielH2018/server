#!/usr/bin/env python3
"""Which deploy tags run a shared k8s role, record or no record.

THE PROBLEM. A shared role — `manifests`, `image-builder`, `volume-claim` — has no
`containers_list` entry, so a `--tags` run cannot select it and no release record names it.
Two readers need the tags that DO run it.

WHO CALLS IT. `deploy_run` reads `expand_shared_tags` to turn `deploy.sh --tags
volume-snapshot` into a deploy of every caller, and `land_tags` reads `caller_tags` to deploy a
shared role's callers instead of reporting `needs-manual-apply`. The deployer runs
`main` as a SUBPROCESS through `deploy_narrow.shared_role_callers`, because this parses YAML
and the unit runs under `uv run --no-project` — the boundary `narrow_setup.py` sits behind for
the same reason. It prints one JSON object, role to sorted `caller_tags`, and
`deploy_defer.discharge_k8s_unapplied` drops a shared role's `k8s_unapplied` line once every
one of those tags carries the change. How a tag proves that, by its release record or
by a matching render, is the deployer's question; this module only names the tags.

Usage: shared_role_callers.py [--repo PATH] ROLE [ROLE ...]
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import argparse
import json

from deploy_tools.narrow_broad import context_for


# DECIDED: transitive. `longhorn-api` and `volume-revert` are called only by shared roles, so a
# walk that stopped at the first hop would name no tag for them and their `k8s_unapplied` lines
# would need a hand to clear. #2704 replaced the land side's first-hop `covered_roles` with this
# for the same reason.
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


# DECIDED: every caller counts, including one whose deploy writes no release record (#3057).
# Until #3057 the deployer's caller set kept only tags whose deploy also ran `manifests`,
# because a caller with no record could never discharge a line and would keep it forever. No
# declared entry has that shape — `cronjob-gate`, `volume-snapshot` and `volume-revert`, the
# three roles that never include `k8s/manifests`, are all shared — so the filter dropped
# nothing and cost a second walk. Should one appear, its shared roles' lines stay until a hand
# clears them, which names the gap rather than discharging past a caller nothing proved.
# `grep -rL k8s/manifests ansible/roles/k8s/*/tasks/main.yml` against the declared entries
# re-derives whether one has.
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("roles", nargs="+", help="role directories under roles/k8s/")
    parser.add_argument(
        "--repo", default=".", help="the checkout to read (default: the cwd)"
    )
    args = parser.parse_args(argv)
    ctx = context_for("HEAD", args.repo)
    print(
        json.dumps(
            {
                role: sorted(caller_tags(role, ctx.declared, ctx.callers))
                for role in args.roles
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    _sys.exit(main())
