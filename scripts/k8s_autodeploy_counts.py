#!/usr/bin/env python3
"""Print the k8s auto-deploy eligible and denylist counts, measured off the tree.

Run: uv run python scripts/k8s_autodeploy_counts.py

This project has stated a wrong count in a PR body three times running (slice 4, slice 7a,
and the slice 7b plan) — print the counts instead of computing them by hand.

Both numbers read the same source `ansible/filter_plugins/k8s_autodeploy.py` derives the
gitops_deploy denylist from, and the same source `ansible/tests/test_k8s_autodeploy_guard.py`'s
`_auto_deployable` reads for eligibility: each role's own `k8s_autodeploy` declaration in
`ansible/roles/k8s/<role>/defaults/main.yml`. The guard test's
`test_every_role_is_either_denied_or_declares_itself_deployable` asserts the two sets partition
every role exactly, so eligible + denylist always equals the role count.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "ansible/filter_plugins"))

import yaml  # noqa: E402
from k8s_autodeploy import SHARED_ROLES, k8s_autodeploy_denylist  # noqa: E402

_ROLES_DIR = _REPO / "ansible/roles/k8s"


def eligible_roles() -> list[str]:
    """Role names declaring `k8s_autodeploy: true` — mirrors the guard test's `_auto_deployable`."""
    names = []
    for entry in sorted(_ROLES_DIR.iterdir()):
        if not entry.is_dir() or entry.name in SHARED_ROLES:
            continue
        defaults = entry / "defaults" / "main.yml"
        if not defaults.is_file():
            continue
        data = yaml.safe_load(defaults.read_text()) or {}
        if bool(data.get("k8s_autodeploy")):
            names.append(entry.name)
    return names


def main() -> None:
    eligible = eligible_roles()
    denied = k8s_autodeploy_denylist(str(_REPO / "ansible"))
    print(f"eligible: {len(eligible)}")
    print(f"denylist: {len(denied)}")


if __name__ == "__main__":
    main()
