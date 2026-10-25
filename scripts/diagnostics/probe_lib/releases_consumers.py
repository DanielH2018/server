"""Which services a byte-supplying shared k8s role can actually make stale.

`releases.manifest_affecting_shared_roles()` answers "which entry-less roles under
`ansible/roles/k8s/` supply bytes to somebody's applied manifests". It does not answer whose,
and `role_paths_for` used the first answer for the second: every census role sat on every
service's path list. Without that, an edit to `game-stats-lib/files/stats_lib.py` would mark
sonarr, traefik, uptime-kuma and wg-easy stale for a file none of them embeds, and `Release
Staleness Drift` would stay DOWN with no deploy tag able to clear it.

THE EDGE IS ALREADY DERIVED ONCE. `lib.k8s_roles.role_callers` reads it: one k8s role reaches
another's tasks either by `include_role: k8s/<role>` or by `import_tasks` of a sibling role's
tasks file, and whoever deploys the caller runs the callee. `land_tags.py` asks it the same
question for the same reason. Re-deriving it here by grepping each role's tree for the
shared role's name would read prose as an edge: `valheim-stats/tasks/main.yml` says "No
volume-claim include, unlike terraria-stats" and `game-stats-lib/tasks/stage.yml` says
"k8s/volume-claim and k8s/manifests", and both would become volume-claim consumers on the
strength of a comment.

FAILING TOWARD VISIBLE. This narrows, so a consumer it misses reads CLEAN -- the false-GREEN
class this reader exists to catch. Two guards keep the failure loud instead:

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

from lib.repo_paths import GITOPS_DEPLOY_FILES, K8S_ROLES

# The build-context table is the deployer's own, so the census and the tick name the same
# role for a file outside every role (#4840).
_sys.path.insert(0, str(GITOPS_DEPLOY_FILES))
from deployer_lib import deploy_cross_role


def role_paths_for(service, shared_roles, consumers=None):
    """The paths whose history decides whether `service` is stale.

    `consumers` is `consumers_for`'s mapping: a shared role listed there joins this list only
    for the services that reach its tasks. A role the mapping does not answer for -- the
    renderer, or one nothing reaches -- joins every service's list, as does every role when
    the caller passes no mapping at all. The default widens.

    The list ends with each file outside `ansible/roles/k8s/` that the service's image embeds,
    from `K8S_BUILD_CONTEXT_FILES`. An edit to one moves the image's content tag, so without it
    a drift that file caused was reported under whatever unrelated path also moved (#4840).
    """
    consumers = consumers or {}
    build_context = sorted(
        path
        for path, roles in deploy_cross_role.K8S_BUILD_CONTEXT_FILES.items()
        if service in roles
    )
    return (
        [f"ansible/roles/k8s/{service}/"]
        + [
            f"ansible/roles/k8s/{r}/"
            for r in sorted(shared_roles)
            # `{service}` as the default is the widening: an unanswered role contains it.
            if service in consumers.get(r, {service})
        ]
        + build_context
    )


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


def consumes_manifests(role_dir):
    """Whether `role_dir`'s tasks include `k8s/manifests`, the contract that ends in a stamp.

    Not every `containers_list` k8s entry has to: an entry whose role only calls
    `k8s/image-builder` and applies no manifests of its own can never be stamped and would
    otherwise read as permanently missing -- the exact "monitor nobody trusts" failure
    `manifest-prune-check.sh.j2`'s header warns against. Same one-level grep the repo CLAUDE.md
    names for this question (`grep -rl k8s/manifests ansible/roles/k8s/*/tasks/`).
    """
    tasks_dir = role_dir / "tasks"
    if not tasks_dir.is_dir():
        return False
    return any("k8s/manifests" in p.read_text() for p in tasks_dir.glob("*.yml"))


def missing_services(records, host_vars=None, k8s_roles_dir=None):
    """k8s-platform service tags, expected to be release-stamped, with no record at all.

    Deployed before the release stamp shipped, or never deployed -- either way this must read
    UNKNOWN rather than being silently excluded from a fleet audit. Scoped to roles that
    actually consume `k8s/manifests` (see `consumes_manifests`); a role that never applies
    manifests never gets a record to be missing. A tag whose entry carries `owner` is never
    missing either: Flux applies it, so no Ansible deploy writes it a record (D2.4b).
    """
    # Deferred for the reason `releases._deploy_tags` gives.
    from deploy_tools.deploy_lib import tags as deploy_tags
    from diagnostics.probe_lib.releases_retired import tree_owners

    host_vars = host_vars or deploy_tags.HOST_VARS
    k8s_roles_dir = k8s_roles_dir or K8S_ROLES
    declared = [
        (host, tag)
        for host, platform, tag in deploy_tags.service_records(host_vars)
        if platform == "k8s"
    ]
    owned = {
        tag
        for host in {host for host, _tag in declared}
        for tag in tree_owners(host_vars / f"{host}.yml")
    }
    known = {
        tag
        for _host, tag in declared
        if tag not in owned and consumes_manifests(k8s_roles_dir / tag)
    }
    present = {r.get("service") for r in records if "error" not in r}
    return sorted(known - present)
