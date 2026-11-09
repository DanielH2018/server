#!/usr/bin/env python3
"""Which services a changed-path list reaches, answered in one module (#3660).

`reach(paths, quiet)` returns a `Reach`. Every tool that asks this question of a path list
reads its answer off that one value: `deploy.sh --changed`, `deploy.sh`'s staleness refusal,
`deploy_tags.py blockers` and the landing in `land.sh`. Behind it sit the deployer's own mapper
(`deploy_changes.services_from_changed_paths`), its test-path rule, the quiet-path filter and
the path-to-role mapper.

THREE READINGS, NOT ONE. The callers ask three different questions of the same paths:

- `Reach.changes` is the deployer's `ChangeSet`, which `deploy.sh --changed` reads.
- `Reach.tags` is the landing's tag list: each path's own declared role. `promote` and Flux
  apply every k8s change, and the landing gates what the promotion's changed set names, so
  this list derives no reader of a changed file.
- `Reach.touched` is everything a path could re-render, for the staleness refusal. It is the
  wider of the last two, because being behind on a path that MIGHT reach a service is the
  reversion that refusal exists to stop.

It imports nothing from `scripts/deploy_tools/` and nothing above `lib/`. `deploy_tags`,
`land_shared`, `land_tags`, `land_reach` and `deploy_staleness` all import it, and
`scripts/tests/test_scripts_import_direction.py` refuses a cycle through any of them.

Run: uv run pytest scripts/deploy_tools/tests/test_reach.py
"""

import sys
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.repo_paths import GITOPS_DEPLOY_FILES, HOST_LIB_FILES, REPO

sys.path.insert(0, str(GITOPS_DEPLOY_FILES))


sys.path.insert(0, str(HOST_LIB_FILES))
from deployer_lib.deploy_logic import (
    ChangeSet,
    _BROAD_MANUAL_PREFIXES,
    _is_test_only_path,
    role_of,
    services_from_changed_paths,
    shared_module_consumers,
)


def role_for(path: str) -> str | None:
    """The role directory a changed path belongs to, or None.

    Not the same question as `tag_for`: a role directory under roles/k8s/ need not have a
    `containers_list` entry, and eight of them do not.

    A `.md` under a role belongs to no role HERE, which is the answer the deployer's own
    mapper gives: `services_from_changed_paths` drops one ahead of every plane branch,
    because a document is not something a playbook applies.
    `test_land_tags_shared_mapper_agreement.py` pins the two answers together.

    The path shape itself is the deployer's `deploy_changes.role_of`, reached through the
    index, so one function answers which role a path sits in. The `.md` rule stays HERE:
    `role_of` is the plain mapper, and this is the caller that reads DIFF paths rather than a
    tree at a ref.
    """
    if path.endswith(".md"):
        return None
    at = role_of(path)
    if at is None or at.plane != "k8s":
        return None
    return at.role


def is_role_test_path(path: str) -> bool:
    """Whether a changed path is test-suite material, which no deploy applies.

    The deployer's own `deploy_changes._is_test_only_path`, called rather than restated, so
    the landing and the tick drop the same paths before they map one to a tag. It is
    only asked of a path `role_for` has already named a role for, where it answers for the
    role's own `tests/` and for a `test_*.py` or `conftest.py` anywhere in the role. Nothing
    stages those: the `no-role-ships-a-pytest-file` row of
    `ansible/tests/repo/test_census_rows_roles.py` holds that tree-wide.

    `tasks/` is NOT dropped, for two reasons. A helper's tasks apply live state to each
    caller separately — arr-notification's seed a Discord Connect notification into the *arr's
    own database. And `_supplies_manifest_bytes` puts `image-builder` in the reported set by
    name: the role ships `templates/build-job.yaml.j2`.

    Read HERE rather than folded into `role_for`, which stays the plain "which role directory is
    this path in" mapper `test_land_tags_shared_mapper_agreement.py` pins against the deployer's
    own `services_from_changed_paths`.
    """
    return _is_test_only_path(path)


def tag_for(path: str, declared: set[str]) -> str | None:
    """The deploy tag a changed path maps to, or None.

    A role's own `tests/` maps to no tag. Dropping it only from `land_shared.shared_roles`
    would leave land.sh DEPLOYING it: a tests-only PR to a declared role would cost a rollout,
    a restart window and a health gate for pytest guards nothing stages to the cluster.
    """
    role = role_for(path)
    # DECIDED: the tag is dropped, not only the shared-role note. The tick re-asserts a role's
    # current manifests on its next image bump anyway, and a pytest guard reaches no cluster.
    if role is None or is_role_test_path(path):
        return None
    return role if role in declared else None


class Reach(NamedTuple):
    """What a changed-path list reaches once the quiet paths are dropped.

    Attributes:
      changes: the deployer's own `ChangeSet` over the loud paths. `.setup_roles`,
        `.broad_deploy`, `.secrets` and `.flux` are what the callers read off it.
      manual: the loud paths under `_BROAD_MANUAL_PREFIXES`. These are the bring-up
        playbooks, which run by hand by construction and park the tick outright.
      loud: the path list with the quiet paths removed, in input order.
      quiet: the paths the caller declared quiet, as given.
    """

    changes: ChangeSet
    manual: list[str]
    loud: list[str]
    quiet: frozenset[str]

    def tags(self, declared: set[str]) -> set[str]:
        """The deploy tags the loud paths map to, before any shared-role expansion."""
        return {t for p in self.loud if (t := tag_for(p, declared))}

    def touched(self, repo: Path | str = REPO) -> set[str]:
        """Every role whose rendered output a loud path could change.

        The `ChangeSet`'s k8s roles (`flux`), plus each role importing a changed `files/*.py` another
        role owns. Wider than `tags` by design: being behind on an import consumer still
        renders an old file.
        """
        return self.changes.flux | shared_module_consumers(self.loud, repo)


def reach(paths, quiet=()) -> Reach:
    """`paths` minus `quiet`, read through the deployer's own mapper.

    Args:
      paths: the changed repo-relative paths: a PR's file list, or a range's diff.
      quiet: the broad-plane paths whose diff carries no content change, from
        `deploy_tags.comment_only_paths`. A playbook named for three edited comments has
        nothing to apply, and PR #843 ended `needs-manual-apply` for exactly that.
    """
    quiet = frozenset(quiet)
    loud = [p for p in paths if p not in quiet]
    return Reach(
        services_from_changed_paths(loud),
        [p for p in loud if p.startswith(_BROAD_MANUAL_PREFIXES)],
        loud,
        quiet,
    )
