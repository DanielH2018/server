#!/usr/bin/env python3
"""Which role a changed path belongs to, the shared-role expansion, and the #3124 narrowing.

A shared k8s role — `manifests`, `volume-claim`, `arr-notification` — has no `containers_list`
entry, so `--tags` cannot select it. `deploy.yml` runs it under the tag of every role that
includes it, so the landing deploys all of those instead of reporting the role to a hand
(#2704, #1397).

ONE EXCEPTION. For a change confined to `roles/k8s/manifests/tasks/` that expansion was 57 tags
and about 20 minutes, and it moved no live object: PR #3117 changed the condition the config
rollout-restart reads, ended `VERDICT: settled`, and 0 of 60 release records showed a restart.
The root `CLAUDE.md` *When to wait* already says a `tasks/`-only change is skipped
deliberately, and the expansion was overriding that. Such a landing now deploys ONE
representative caller as a smoke test, and `shared_role_callers.smoke_caller` carries the
`# DECIDED:` holding the operator's ruling and the gap it accepts.

WHY IT IS NOT IN `land_tags`. That module is at its 600-line cap, and
`scripts/tests/test_scripts_import_direction.py` refuses a cycle — so the mappers the shared-role
half reads moved here with it rather than being imported back. `land_tags` re-exports every name
below, so each existing reader keeps reading it there.

Run: uv run pytest scripts/deploy_tools/tests/test_shared_role_smoke_caller.py
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import deploy_tags
from lib.k8s_roles import role_callers
from lib.repo_paths import GITOPS_DEPLOY_FILES
from shared_role_callers import SMOKE_TESTABLE_SHARED_ROLES, caller_tags, smoke_caller

_sys.path.insert(0, str(GITOPS_DEPLOY_FILES))

from deploy_logic import role_of


# The one directory under the role trees that is not a service: `common`, the shared Docker
# deploy path. `--tags common` matches no containers_list entry, and Ansible exits 0 on a tag
# selecting nothing. `archive` sat here too until #2540 — it classified DIFF paths rather than
# a tree at a ref, so it had to outlive the tree #2385 deleted until the deployer's `local`
# was past that merge commit.
_NOT_SERVICES = frozenset({"common"})


def declared_tags() -> set[str]:
    """Every name that selects a service, read from containers_list."""
    return deploy_tags.service_tags()


def role_for(path: str) -> str | None:
    """The role directory a changed path belongs to, or None.

    Not the same question as `tag_for`: a role directory under roles/k8s/ need not have a
    `containers_list` entry, and eight of them do not.

    A `.md` under a role belongs to no role HERE, which is the answer the deployer's own
    mapper already gives: `services_from_changed_paths` drops one ahead of every plane branch,
    because a document is not something a playbook applies. This mapper did not, so a PR whose
    only change under `roles/k8s/manifests/` was that role's CLAUDE.md came out of
    `shared_roles` as a tag-less role owed to a hand, and land.sh ended `needs-manual-apply`
    asking for a full `ansible/deploy.yml` run for prose (issue #1701).
    `test_land_tags_shared_mapper_agreement.py` pins the two answers together.

    The path shape itself is the deployer's `deploy_changes.role_of`, reached through the
    index, so one function answers which role a path sits in (#3048). The `.md` rule and
    `_NOT_SERVICES` stay HERE: `role_of` is the plain mapper, and this is the caller that reads
    DIFF paths rather than a tree at a ref.
    """
    if path.endswith(".md"):
        return None
    at = role_of(path)
    if at is None or at.plane == "setup" or at.role in _NOT_SERVICES:
        return None
    return at.role


def is_role_test_path(path: str) -> bool:
    """Whether a changed path is a role's own `tests/` file.

    Answers about segment 4 of `ansible/roles/<plane>/<role>/<sub>/...` alone, so it is only
    meaningful for a path `role_for` has already named a role for. `shared_roles` and `tag_for`
    are the callers, and both drop such a path: a role's `tests/` is work no deploy applies, the same class as the
    `.md` rule in `role_for` (#1701). Pytest guards over the role's `files/*.py` are staged by
    nothing — `ansible/tests/repo/test_no_role_ships_a_test_file.py` holds that tree-wide — so
    they reach no cluster and no deploy can apply them. `_is_real_change` in
    `scripts/diagnostics/probe_lib/releases.py` drops a role's `tests/` for that reason.

    `tasks/` is NOT dropped, which is what issue #1729 proposed and three facts refute. A role
    with no `containers_list` entry and no caller has no path to being applied at all, and a
    tasks-only PR adding one must still be reported
    (`tests/test_land_classify.py:110`, issue #1544). A helper's tasks apply live state to each
    caller separately — arr-notification's seed a Discord Connect notification into the *arr's
    own database — so deploying one caller is not the change applied
    (`tests/test_land_tags_caller_coverage.py:76`, issue #1397). And `_supplies_manifest_bytes`,
    the predicate #1729 cites as its precedent, puts `volume-claim` in the reported set by name:
    the role ships `templates/pvc.yaml.j2`.

    Read HERE rather than folded into `role_for`, which stays the plain "which role directory is
    this path in" mapper `test_land_tags_shared_mapper_agreement.py` pins against the deployer's
    own `services_from_changed_paths`. The segment itself comes off `role_of`, which already
    carries it, rather than off a fourth `split("/")` of the same path (#3048).
    """
    at = role_of(path)
    return at is not None and at.subdir == "tests"


# Subdirectories of a shared role whose content decides how a deploy RUNS rather than what it
# applies. `shared_role_reach._DEPLOY_TIME_SUBDIRS` asks the same question one level finer, over
# the diff; this is the path rule, so it answers with no range to read.
#
# `meta/` is deliberately absent, where that set carries it: a dependency change alters which
# OTHER roles run for every caller, and one caller cannot smoke-test that. `handlers/` is in,
# because a handler of this role runs inside this role for whichever caller triggers it.
_DEPLOY_RUN_SUBDIRS = frozenset({"tasks", "handlers"})


def shared_roles(files, declared: set[str] | None = None) -> list[str]:
    """The changed role directories that have no `containers_list` entry.

    These are the shared k3s plane — `manifests` is the apply-and-roll path every workload
    includes, `volume-claim` and `volume-revert` are storage paths several include. Naming one
    in `--tags` makes deploy.sh refuse the ENTIRE list (exit 2), so they must be split off the
    tags and reported as work a human still owes. PR #617 is the measured case.

    A role's own `tests/` does not put it here at all — `is_role_test_path`.
    """
    declared = declared_tags() if declared is None else declared
    roles = {r for p in files if (r := role_for(p)) and not is_role_test_path(p)}
    return sorted(roles - declared)


def shared_caller_tags(files, declared: set[str] | None = None) -> dict[str, set[str]]:
    """For each shared role `files` changes, the tags of the roles that run it (#2704).

    A helper role has no tag of its own, but `deploy.yml` runs it under the tag of every role
    whose tasks include it, so deploying all of its callers applies it (#1397). All of them,
    not one: PR #617 deployed 22 of `manifests`' callers, which re-applied it for those 22
    alone. Transitive, so `longhorn-api` reaches the services behind `volume-snapshot`.

    `narrowed` is the one exception, and the module docstring has its argument (#3124).

    The landing deploys these tags, and `land_tags.plane_note` drops every role this gives a
    non-empty set. An empty set is a role nothing deploys, which stays in the note.
    """
    declared = declared_tags() if declared is None else declared
    callers = role_callers()
    return {
        r: narrowed(r, caller_tags(r, declared, callers), files)
        for r in shared_roles(files, declared)
    }


def deploy_run_only(role: str, files) -> bool:
    """Whether every changed path that puts `role` in `shared_roles` is deploy-run logic.

    The same path selection `shared_roles` makes — `role_for` drops a `.md` and
    `is_role_test_path` drops the role's own `tests/`, so PR #3117's `manifests/CLAUDE.md` and
    its `ansible/tests/` files decide nothing here. What is left must all sit in
    `_DEPLOY_RUN_SUBDIRS`. A `templates/` or `files/` path is bytes a deploy applies, and a
    `defaults/` key can render into a manifest, so either keeps the full fan-out.

    Args:
        role: the shared role's directory name.
        files: the PR's changed paths, the whole list.

    Returns:
        False for a role with no deciding path at all, which keeps every caller.
    """
    subdirs = set()
    for path in files:
        if role_for(path) != role or is_role_test_path(path):
            continue
        at = role_of(path)
        subdirs.add(at.subdir if at else "")
    return bool(subdirs) and subdirs <= _DEPLOY_RUN_SUBDIRS


def narrowed(role: str, tags: set[str], files) -> set[str]:
    """`tags`, or the one smoke caller where the narrowing applies. NEVER empty.

    Empty is not a valid answer: `land_tags.plane_note` reads an empty caller set as a role
    nothing deploys and asks a hand for a full `ansible/deploy.yml`. A `smoke_caller` that
    resolves to None therefore keeps every caller.

    Args:
        role: the shared role's directory name.
        tags: its caller tags, as `shared_role_callers.caller_tags` derives them.
        files: the PR's changed paths, the whole list.
    """
    if role not in SMOKE_TESTABLE_SHARED_ROLES or len(tags) < 2:
        return tags
    if not deploy_run_only(role, files):
        return tags
    one = smoke_caller(tags)
    return {one} if one else tags


def narrowed_roles(files, declared: set[str] | None = None) -> frozenset[str]:
    """The shared roles `shared_caller_tags` narrowed to one smoke caller.

    What `land_lib.classify` says to the operator is read off this. A line claiming that every
    service running the role was deployed is the failure mode the narrowing introduces, and it
    cannot be derived from the tag count alone — a role with one caller is not narrowed.
    """
    declared = declared_tags() if declared is None else declared
    callers = role_callers()
    out = set()
    for role in shared_roles(files, declared):
        tags = caller_tags(role, declared, callers)
        if tags and narrowed(role, tags, files) != tags:
            out.add(role)
    return frozenset(out)
