#!/usr/bin/env python3
"""Guards that renovate.json disables a built-in manager by name, never through `enabledManagers`.

Renovate runs built-in managers alongside custom ones, and both can match the same file. When they
do, every bump opens TWO PRs, and only one carries the packageRules keyed on `matchManagers:
["custom.regex"]`. The fix is to disable the one built-in manager as its own object.

Run: uv run pytest scripts/tests/test_renovate_manager_overlap.py
"""

import json

from lib.repo_paths import REPO

_CONFIG = json.loads((REPO / "renovate.json").read_text())


def test_enabled_managers_is_not_used_as_the_fix() -> None:
    """`enabledManagers` is an allowlist, and this repo needs four built-in managers.

    Reaching for it to silence an overlap would disable `dockerfile`, `github-actions`,
    `pep621` and `pyenv` in one stroke — three of which have lockstep tests in
    test_renovate_managers.py that would then be guarding pins nothing updates.
    """
    assert "enabledManagers" not in _CONFIG, (
        "enabledManagers is an allowlist: adding it disables every built-in manager not named, "
        "including dockerfile, github-actions, pep621 and pyenv. Disable a specific manager with "
        'its own object instead — "<manager>": {"enabled": false}'
    )
