#!/usr/bin/env python3
"""The shared-role expansion a landing makes, and the #3124 narrowing.

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
`scripts/tests/test_scripts_import_direction.py` refuses a cycle — so this reads the path mappers
from `reach`, which sits below both, rather than from `land_tags`. `land_tags` re-exports every
name below, so each existing reader keeps reading it there.

Run: uv run pytest scripts/deploy_tools/tests/test_shared_role_smoke_caller.py
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
_sys.path.insert(1, str(_Path(__file__).resolve().parents[1]))  # scripts/deploy_tools

from deploy_tools.deploy_lib import tags as deploy_tags
from lib.k8s_roles import role_callers
from lib.repo_paths import GITOPS_DEPLOY_FILES, ROLES
from shared_role_callers import SMOKE_TESTABLE_SHARED_ROLES, caller_tags, smoke_caller

_sys.path.insert(0, str(GITOPS_DEPLOY_FILES))

from deploy_logic import role_of

# The path-to-role mapper and the test-path rule live in `reach`, the module that answers
# which services a path list reaches. `land_tags` re-exports both under their old names.
from reach import is_role_test_path, role_for


def declared_tags() -> set[str]:
    """Every name that selects a service, read from containers_list."""
    return deploy_tags.service_tags()


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
