#!/usr/bin/env python3
"""Which role a changed path belongs to, the shared-role expansion, and the #3124 narrowing.

A shared k8s role — `manifests`, `image-builder`, `arr-notification` — has no `containers_list`
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
from lib.repo_paths import GITOPS_DEPLOY_FILES, ROLES
from shared_role_callers import SMOKE_TESTABLE_SHARED_ROLES, caller_tags, smoke_caller

_sys.path.insert(0, str(GITOPS_DEPLOY_FILES))

from deploy_logic import _is_test_only_path, role_of


# The one directory under the role trees that is not a service: `common`, the shared Docker
# deploy path. `--tags common` matches no containers_list entry, and Ansible exits 0 on a tag
# selecting nothing.
_NOT_SERVICES = frozenset({"common"})


def declared_tags() -> set[str]:
    """Every name that selects a service, read from containers_list."""
    return deploy_tags.service_tags()


def role_for(path: str) -> str | None:
    """The role directory a changed path belongs to, or None.

    Not the same question as `tag_for`: a role directory under roles/k8s/ need not have a
    `containers_list` entry, and eight of them do not.

    A `.md` under a role belongs to no role HERE, which is the answer the deployer's own
    mapper gives: `services_from_changed_paths` drops one ahead of every plane branch,
    because a document is not something a playbook applies.
    `test_land_tags_shared_mapper_agreement.py` pins the two answers together.

    The path shape itself is the deployer's `deploy_changes.role_of`, reached through the
    index, so one function answers which role a path sits in. The `.md` rule and
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
    """Whether a changed path is test-suite material, which no deploy applies.

    The deployer's own `deploy_changes._is_test_only_path`, called rather than restated, so
    the landing and the tick drop the same paths before they map one to a tag (#3660). It is
    only asked of a path `role_for` has already named a role for, where it answers for the
    role's own `tests/` and for a `test_*.py` or `conftest.py` anywhere in the role. Nothing
    stages those: the `no-role-ships-a-test-file` row of
    `ansible/tests/repo/test_census_rows_roles.py` holds that tree-wide, and
    `_is_real_change` in `scripts/diagnostics/probe_lib/releases.py` drops them for the same
    reason. `shared_roles` and `tag_for` are the callers, and both drop such a path, the same
    class as the `.md` rule in `role_for`.

    `tasks/` is NOT dropped, for three reasons. A role
    with no `containers_list` entry and no caller has no path to being applied at all, and a
    tasks-only PR adding one must still be reported
    (`tests/test_land_classify.py:110`). A helper's tasks apply live state to each
    caller separately — arr-notification's seed a Discord Connect notification into the *arr's
    own database — so deploying one caller is not the change applied
    (`tests/test_land_tags_caller_coverage.py:76`). And `_supplies_manifest_bytes`
    puts `image-builder` in the reported set by name:
    the role ships `templates/build-job.yaml.j2`.

    Read HERE rather than folded into `role_for`, which stays the plain "which role directory is
    this path in" mapper `test_land_tags_shared_mapper_agreement.py` pins against the deployer's
    own `services_from_changed_paths`.
    """
    return _is_test_only_path(path)


# Subdirectories of a shared role whose content decides how a deploy RUNS rather than what it
# applies. `shared_role_reach._DEPLOY_TIME_SUBDIRS` asks the same question one level finer, over
# the diff; this is the path rule, so it answers with no range to read.
#
# `meta/` is deliberately absent, where that set carries it: a dependency change alters which
# OTHER roles run for every caller, and one caller cannot smoke-test that. `handlers/` is in,
# because a handler of this role runs inside this role for whichever caller triggers it.
_DEPLOY_RUN_SUBDIRS = frozenset({"tasks", "handlers"})


def shared_roles(
    files, declared: set[str] | None = None, roles_root: _Path | None = None
) -> list[str]:
    """The changed role directories that have no `containers_list` entry.

    These are the shared k3s plane — `manifests` is the apply-and-roll path every workload
    includes, `volume-snapshot` and `volume-revert` are storage paths several include. Naming one
    in `--tags` makes deploy.sh refuse the ENTIRE list (exit 2), so they must be split off the
    tags and reported as work a human still owes.

    A role's own `tests/` does not put it here at all — `is_role_test_path`.

    Nor does a role the change DELETED. Its directory is gone from the tree this reads, so no
    play can run it: an `include_role` still naming it fails with "role not found" rather than
    applying anything. Retiring `k8s/volume-claim` (#3387) otherwise reported a full
    `ansible/deploy.yml` as owed for a role that had no callers left to deploy.
    """
    declared = declared_tags() if declared is None else declared
    roles_root = ROLES if roles_root is None else roles_root
    roles = set()
    for p in files:
        role = role_for(p)
        at = role_of(p)
        if not role or at is None or is_role_test_path(p):
            continue
        if (roles_root / at.plane / role).is_dir():
            roles.add(role)
    return sorted(roles - declared)


def shared_caller_tags(
    files, declared: set[str] | None = None, roles_root: _Path | None = None
) -> dict[str, set[str]]:
    """For each shared role `files` changes, the tags of the roles that run it.

    A helper role has no tag of its own, but `deploy.yml` runs it under the tag of every role
    whose tasks include it, so deploying all of its callers applies it. All of them,
    not one: a caller deployed alone re-applies the helper for that caller only.
    Transitive, so `longhorn-api` reaches the services behind `volume-snapshot`.

    `narrowed` is the one exception, and the module docstring has its argument.

    The landing deploys these tags, and `land_tags.plane_note` drops every role this gives a
    non-empty set. An empty set is a role nothing deploys, which stays in the note.
    """
    declared = declared_tags() if declared is None else declared
    callers = role_callers()
    return {
        r: narrowed(r, caller_tags(r, declared, callers), files)
        for r in shared_roles(files, declared, roles_root)
    }


def deploy_run_only(role: str, files) -> bool:
    """Whether every changed path that puts `role` in `shared_roles` is deploy-run logic.

    The same path selection `shared_roles` makes — `role_for` drops a `.md` and
    `is_role_test_path` drops the role's own `tests/`, so PR #3117's `manifests/CLAUDE.md` and
    its `ansible/tests/` files decide nothing here. What is left must all sit in
    `_DEPLOY_RUN_SUBDIRS`. A `templates/` or `files/` path is bytes a deploy applies, and a
    `defaults/` key can render into a manifest, so either keeps the full fan-out.

    THE SPLIT WITH `shared_role_reach.deploy_time_only` (#3150). Both ask whether a shared-role
    change moves a rendered manifest, for different callers. That one decides whether a hand
    owes a full `ansible/deploy.yml` at all. This one decides whether the landing's caller
    deploy can shrink to one smoke caller. That one is the finer rule: it reads the diff over a
    range, per changed key. This one is a path rule over the file list alone, so it answers
    with no range and cannot fail on a git read. `meta/` is the one disagreement, and it is
    deliberate. That rule can find a comment-only `meta/` edit quiet, but here any `meta/`
    path keeps the full fan-out: a dependency change alters which other roles run for every
    caller, and one caller cannot smoke-test that. A rule about WHICH BYTES
    a change moves goes there; a rule about how many callers prove a change runs goes here.
    `tests/test_shared_role_smoke_caller.py` holds this set inside that one's.

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
