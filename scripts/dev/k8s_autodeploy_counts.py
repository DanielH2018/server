#!/usr/bin/env python3
"""Print the k8s `k8s_autodeploy` stance counts, or the denylist, measured off the tree.

Run: uv run python scripts/dev/k8s_autodeploy_counts.py [--denylist]

This project has stated a wrong count in a PR body three times running (slice 4, slice 7a,
and the slice 7b plan) — print the counts instead of computing them by hand.

`--denylist` prints the roles declaring `k8s_autodeploy: false`, comma-separated, and exits 1
when the derivation raises. `land_renovate_pr.py` reads that output as its denylist, so it and
renovate.json's no-automerge rule read one list.

Both counts read `lib.k8s_roles.k8s_autodeploy_denylist` and the same source
`tests/deploy/_autodeploy.py`'s `_auto_deployable` reads for eligibility: each role's
own `k8s_autodeploy` declaration in `k8s/roles/<role>/defaults/main.yml`. The guard test's
`test_every_role_is_either_denied_or_declares_itself_deployable` asserts the two sets partition
every role exactly, so eligible + denylist always equals the role count.
"""

from collections.abc import Sequence
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.repo_paths import K8S_ROLES

from lib import yaml_fast
from lib.cli_help import answer_help
from lib.k8s_roles import k8s_autodeploy_denylist, role_dirs


def eligible_roles() -> list[str]:
    """Role names declaring `k8s_autodeploy: true` — mirrors the guard test's `_auto_deployable`."""
    names = []
    for entry in role_dirs(K8S_ROLES):
        defaults = entry / "defaults" / "main.yml"
        if not defaults.is_file():
            continue
        data = yaml_fast.safe_load(defaults.read_text()) or {}
        if bool(data.get("k8s_autodeploy")):
            names.append(entry.name)
    return names


def all_role_names() -> list[str]:
    """Every role directory under `k8s/roles/`, sorted, debris excluded."""
    return [entry.name for entry in role_dirs(K8S_ROLES)]


def autodeploy_stances() -> tuple[list[str], list[str], list[str]]:
    """Every k8s role, partitioned into eligible / denied / not-declaring.

    Eligible and denied come from `eligible_roles()` and `k8s_autodeploy_denylist()`, which
    `test_every_role_is_either_denied_or_declares_itself_deployable` asserts partition every
    role that declares a stance. Not-declaring is the remainder, which `k8s_autodeploy_denylist`
    refuses, so it reads empty on the tree.

    Returns:
        `(eligible, denied, not_declaring)`, each sorted.
    """
    eligible = eligible_roles()
    denied = k8s_autodeploy_denylist()
    not_declaring = sorted(set(all_role_names()) - set(eligible) - set(denied))
    return eligible, denied, not_declaring


def print_denylist(roles: Path = K8S_ROLES) -> int:
    """Print the denylist comma-separated, and return the exit code."""
    try:
        denied = k8s_autodeploy_denylist(roles)
    except ValueError as exc:
        print(f"k8s_autodeploy_counts.py: {exc}", file=sys.stderr)
        return 1
    print(",".join(denied))
    return 0


def main(argv: Sequence[str] = ()) -> int:
    answer_help(__doc__, list(argv))
    if "--denylist" in argv:
        return print_denylist()
    eligible, denied, not_declaring = autodeploy_stances()
    print(f"eligible: {len(eligible)}")
    print(f"denylist: {len(denied)}")
    print(f"not declaring: {len(not_declaring)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
