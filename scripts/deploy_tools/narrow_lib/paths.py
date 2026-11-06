#!/usr/bin/env python3
"""The one path-shape rule `narrow_setup` shares with the tick: what counts as prose."""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/

from lib.repo_paths import GITOPS_DEPLOY_FILES, HOST_LIB_FILES

# The deployer's own `files/`, for the one rule this module shares with the tick itself. Reached
# as a path entry rather than a copy; the import
# stays inside `is_prose`, because only the path entry is free at module import.
_sys.path.insert(0, str(GITOPS_DEPLOY_FILES))


_sys.path.insert(0, str(HOST_LIB_FILES))


def is_prose(path: str) -> bool:
    """Whether a changed path is documentation no playbook applies.

    `land_tags.role_for` drops a `.md` for the same reason, and so does
    `role_reads.sort_hits` for a grep hit.

    The rule itself is the deployer's `deploy_changes.is_doc`, reached through the index, so the
    tick and this derivation cannot disagree about what a `.md` is (#2810). It used to carve out
    a `.md` under `files/` or `templates/` as shippable; the `DECIDED:` on `is_doc` says why that
    carve-out went, and which guard replaced it. `narrow_setup._reaches_no_host` calls this too.
    """
    from deployer_lib.deploy_logic import is_doc

    return is_doc(path)
