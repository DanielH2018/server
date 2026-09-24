"""Which services a byte-supplying shared k8s role can actually make stale.

`releases.manifest_affecting_shared_roles()` answers "which entry-less roles under
`ansible/roles/k8s/` supply bytes to somebody's applied manifests". It does not answer whose,
and `role_paths_for` used the first answer for the second: every census role sat on every
service's path list. So PR #2503's edit to `game-stats-lib/files/stats_lib.py` marked sonarr,
traefik, uptime-kuma and wg-easy stale for a file none of them embeds, and `Release Staleness
Drift` stayed DOWN with no deploy tag able to clear it (#2504) -- the shape #1672 records for
`volume-claim`'s staging-directory move.

THE EDGE IS ALREADY DERIVED ONCE. `lib.k8s_roles.role_callers` reads it: one k8s role reaches
another's tasks either by `include_role: k8s/<role>` or by `import_tasks` of a sibling role's
tasks file, and whoever deploys the caller runs the callee. `land_tags.py` asks it the same
question for the same reason (#1397). Re-deriving it here by grepping each role's tree for the
shared role's name would read prose as an edge: `valheim-stats/tasks/main.yml` says "No
volume-claim include, unlike terraria-stats" and `game-stats-lib/tasks/stage.yml` says
"k8s/volume-claim and k8s/manifests", and both would become volume-claim consumers on the
strength of a comment.

FAILING TOWARD VISIBLE. This narrows, so a consumer it misses reads CLEAN -- the false-GREEN
class issue #947 built this reader to catch. Two guards keep the failure loud instead:

  * `manifests` is fleet-wide by name and never narrowed. Its tasks render every service's
    bytes, and three docstrings in `releases.py` name it as the one path that must never read
    clean.
  * A census role whose caller closure is EMPTY stays fleet-wide. An edge nothing can see is
    not evidence that nothing consumes the role.

The closure is transitive because the edge is: a service that reaches a shared role through
another shared role runs its tasks just the same. No live pair does that today -- both
comments above are exactly the pair that looks like one and is not -- so
`test_probe_releases_consumers.py` drives it on a synthetic tree rather than leaving the
recursion unexercised.
"""

# `probe_lib` is a namespace package under `scripts/`, so reaching `lib.k8s_roles` by package
# name needs `scripts/` on sys.path -- the same bootstrap releases.py carries, for its reason.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))


def role_paths_for(service, shared_roles, consumers=None):
    """The `ansible/roles/k8s/` paths whose history decides whether `service` is stale.

    `consumers` is `consumers_for`'s mapping: a shared role listed there joins this list only
    for the services that reach its tasks. A role the mapping does not answer for -- the
    renderer, or one nothing reaches -- joins every service's list, as does every role when
    the caller passes no mapping at all. The default widens.
    """
    consumers = consumers or {}
    return [f"ansible/roles/k8s/{service}/"] + [
        f"ansible/roles/k8s/{r}/"
        for r in sorted(shared_roles)
        # `{service}` as the default is the widening: an unanswered role contains it.
        if service in consumers.get(r, {service})
    ]


def _closure(role, callers):
    """Every role that reaches `role`'s tasks, directly or through another role."""
    reached, queue = set(), [role]
    while queue:
        callee = queue.pop()
        for caller in callers.get(callee, ()):
            if caller != role and caller not in reached:
                reached.add(caller)
                queue.append(caller)
    return reached


def _role_callers(repo):
    """Import `scripts/lib/k8s_roles` lazily.

    For the reason `releases._deploy_tags` gives: the import pulls in PyYAML and
    `lib.render_guard`, and the call walks every k8s role's `tasks/` and parses each file --
    work only `--stale-only` asks for, not the other `probe.py` subcommands that load
    `releases` through `probe.py`.
    """
    from lib.k8s_roles import role_callers

    return role_callers(repo)


def consumers_for(shared_roles, repo=None, callers=None, renderer="manifests"):
    """{shared role: frozenset(consuming services)} for the census roles that can be narrowed.

    Args:
        shared_roles: `releases.manifest_affecting_shared_roles()`, the byte suppliers.
        repo: the checkout to read the caller graph from. `None` reads this file's own.
        callers: a prebuilt `role_callers()` mapping, for a test that drives the closure
            without a tree.
        renderer: `releases.MANIFEST_RENDERER`, the one role left fleet-wide by name.

    Returns:
        A mapping a role is ABSENT from when it must stay on every service's path list --
        the renderer, and any role nothing reaches. `role_paths_for` reads it that way round
        so that a missing answer widens rather than narrows.
    """
    if callers is None:
        callers = _role_callers(repo)
    narrowed = {}
    for role in shared_roles:
        if role == renderer:
            continue
        # A shared role in the closure is an intermediate, not a service: it has no
        # containers_list entry and so no record to be stale. Its own callers are already in
        # the closure, which is what makes dropping it safe.
        consumers = _closure(role, callers) - set(shared_roles)
        if consumers:
            narrowed[role] = frozenset(consumers)
    return narrowed
