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

THE THREE READERS DIFFER, and `smoke_caller` is why. The deployer's discharge needs every
caller, because the question it asks is whether the change is applied everywhere. A
hand-typed `deploy.sh --tags manifests` deploys every caller too, which the operator ruled on
in #2717. The landing narrows a deploy-run-only change to ONE caller, and the `# DECIDED:` at
`smoke_caller` holds that ruling.

Usage: shared_role_callers.py [--repo PATH] ROLE [ROLE ...]
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import argparse
import json
from pathlib import Path

from deploy_tools.narrow_broad import context_for
from deploy_tools.render_targets import render_targets
from lib.render_guard import HOSTS_LAND_SH_NEVER_DEPLOYS
from lib.repo_paths import HOST_VARS, K8S_ROLES


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


# The shared roles whose whole effect on a caller is the manifests it renders, so one caller
# can smoke-test a change to how it renders them. `deploy_defer.DIGEST_PROVABLE_ROLES` draws
# the same line for the same reason and names only `manifests` too — the two are pinned
# together by `tests/test_shared_role_smoke_caller.py`, so neither can widen alone.
#
# Every other shared role acts per caller and needs all of them: `arr-notification` writes
# each *arr's own database over its API, `volume-claim` stages a PVC in the caller's own
# directory, `image-builder` ships a build job per caller. Deploying one of those is not the
# change applied — `land_tags.shared_caller_tags`' own measured case is PR #1393 (#1397).
SMOKE_TESTABLE_SHARED_ROLES = frozenset({"manifests"})


# DECIDED: a landing narrows a deploy-run-only change to a smoke-testable shared role down to
# ONE caller, the cheapest render target, and lets the rest take effect on their own next
# deploy. Operator ruling 2026-10-01 (#3124), choosing it over the two alternatives in that
# issue: reporting `nothing-to-deploy`, and keeping the full fan-out.
#
# THE MEASURED CASE. PR #3117 changed `manifests/tasks/` alone — the condition the config
# rollout-restart reads. `land_tags.shared_caller_tags` fanned it out to 57 tags and about 20
# minutes, `DEPLOY-VERDICT: deployed` then `VERDICT: settled`, and 0 of 60 release records
# showed a restart. The root `CLAUDE.md` *When to wait* already says a `tasks/`-only change is
# skipped deliberately; the expansion was overriding that for a shared role.
#
# WHY ONE RATHER THAN NONE. Changed task logic that runs on every caller has no other proof
# that it runs without erroring, and a render or an apply flag is exactly what a dry run
# cannot show. One caller buys that proof for one rollout.
#
# WHY ONE IS ENOUGH. `manifests` acts through the bytes it renders, so the other 56 callers
# re-read the new task logic on their own next deploy with nothing left behind — and the
# deployer's `k8s_unapplied` line for `manifests` discharges on a render-digest match without
# any deploy at all (`deploy_defer.DIGEST_PROVABLE_ROLES`, #3057). That marker already accepts
# the one gap this shares: a change to HOW `manifests` applies can leave live state different
# from what the change would produce while no digest moves.
#
# THE ASYMMETRY IS DELIBERATE. `expand_shared_tags` — the hand-typed `deploy.sh --tags
# manifests` — still deploys every caller, because there the operator asked for the fleet
# (#2717), and `deploy_defer.discharge_k8s_unapplied` still reads every caller, because its
# question is whether the change is applied everywhere rather than whether it runs.
#
# WHY THE COST KEY IS A PROXY AND STAYS ONE (#3149). Hosts, then rendered templates, counts
# objects, and rollout wait usually dominates a k8s deploy. It does not dominate this one. The
# narrowing fires only for a `tasks/`/`handlers/`-only change (`land_shared.deploy_run_only`),
# which renders no new bytes, so the central rollout-restart has nothing to fire on — #3117's
# smoke-shaped change restarted 0 of 60 workloads. What is left is the shared play overhead,
# identical for every tag, plus one apply and one wait per rendered object, each already
# converged. That is the part the key counts. No per-tag wall-clock exists to check it against:
# the deployer's journal holds no single-tag apply of any of the five callers tied at one
# template (bento-pdf, dri-device-plugin, littlelink, node-exporter, texbrain), and among those
# the name decides anyway. Replace the template count with a measured number only if one is
# taken and it disagrees by more than run-to-run noise, and derive it rather than listing it.
def smoke_caller(
    tags: set[str], host_vars: Path = HOST_VARS, roles: Path = K8S_ROLES
) -> str | None:
    """The one caller to deploy as a smoke test, or None when no caller qualifies.

    CHEAPEST, derived rather than listed. A candidate must be a render target on a host
    `land.sh` deploys to — declared `platform: k8s` and including `k8s/manifests`, as
    `render_targets` derives it — so the deploy actually exercises the render-and-apply path
    the change is in. Among those, the cost key is the number of hosts declaring it, then the
    number of manifests it renders, then its name: a one-template service on one host is one
    apply of an object that is already converged, and the name breaks the tie so two runs over
    one tree pick the same caller. The `# DECIDED:` above says why that count is not a timing.

    None for an empty candidate set, which is the answer that keeps the full fan-out. An empty
    caller set would read to `land_tags.plane_note` as a role nothing deploys.

    Args:
        tags: the caller tags, as `caller_tags` derives them.
        host_vars: the `inventory/host_vars/` directory to read the declarations from.
        roles: the `roles/k8s/` tree to count rendered templates in.
    """
    hosts: dict[str, int] = {}
    for path in sorted(host_vars.glob("*.yml")):
        if path.stem.startswith("_") or path.stem in HOSTS_LAND_SH_NEVER_DEPLOYS:
            continue
        for name in render_targets(path.stem, host_vars, roles):
            hosts[name] = hosts.get(name, 0) + 1
    candidates = sorted(set(tags) & set(hosts))
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda tag: (
            hosts[tag],
            len(list((roles / tag / "templates").glob("*.j2"))),
            tag,
        ),
    )


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
