#!/usr/bin/env python3
"""Which changed role directories have no `containers_list` entry, and the mappers that say so.

A shared k8s role — `manifests`, `image-builder`, `arr-notification` — has no `containers_list`
entry, so `--tags` cannot select it. `land_tags` re-exports every name below.

WHY IT IS NOT IN `land_tags`. `scripts/tests/test_scripts_import_direction.py` refuses a cycle,
so this reads the mappers from `reach`, which sits below both, rather than from `land_tags`.

Run: uv run pytest scripts/deploy_tools/tests/test_land_tags_shared_mapper_agreement.py
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
_sys.path.insert(1, str(_Path(__file__).resolve().parents[1]))  # scripts/deploy_tools

from deploy_tools.deploy_lib import tags as deploy_tags
from lib.repo_paths import GITOPS_DEPLOY_FILES, HOST_LIB_FILES, ROLES

_sys.path.insert(0, str(GITOPS_DEPLOY_FILES))


_sys.path.insert(0, str(HOST_LIB_FILES))
from deployer_lib.deploy_logic import role_of

# The path-to-role mapper and the test-path rule live in `reach`, the module that answers
# which services a path list reaches. `land_tags` re-exports both under their old names.
from reach import is_role_test_path, role_for


def declared_tags() -> set[str]:
    """Every name that selects a service, read from containers_list."""
    return deploy_tags.service_tags()


def shared_roles(
    files,
    declared: set[str] | None = None,
    roles_root: _Path | None = None,
    role_dirs: frozenset[str] | None = None,
) -> list[str]:
    """The changed role directories that have no `containers_list` entry.

    These are the shared k3s plane — `manifests` is the apply-and-roll path every workload
    includes, `volume-snapshot` and `volume-revert` are storage paths several include. Naming one
    in `--tags` makes deploy.sh refuse the ENTIRE list (exit 2).

    A role's own `tests/` does not put it here at all — `is_role_test_path`.

    Nor does a role the change DELETED. Its directory is gone from the tree this reads, so no
    play can run it: an `include_role` still naming it fails with "role not found" rather than
    applying anything. Retiring `k8s/volume-claim` (#3387) otherwise reported a full
    `ansible/deploy.yml` as owed for a role that had no callers left to deploy.

    `role_dirs` is the `<plane>/<role>` directories at the merge commit, which is the tree
    that decides whether a role is gone: the checkout land.py runs from can predate the merge,
    or keep a deleted role's ignored `__pycache__/` (#7108). None skips that read. It only ever
    removes a role. A role this checkout lacks stays out even when the merge commit adds it,
    because `role_callers` reads this checkout too and would call it caller-less.
    """
    declared = declared_tags() if declared is None else declared
    roles_root = ROLES if roles_root is None else roles_root
    roles = set()
    for p in files:
        role = role_for(p)
        at = role_of(p)
        if not role or at is None or is_role_test_path(p):
            continue
        gone_at_merge = role_dirs is not None and f"{at.plane}/{role}" not in role_dirs
        if (roles_root / at.plane / role).is_dir() and not gone_at_merge:
            roles.add(role)
    return sorted(roles - declared)
