#!/usr/bin/env python3
"""Guards that a built-in Renovate manager never shadows a custom one.

Renovate runs built-in managers alongside custom ones, and both can match the same file. When they
do, every bump opens TWO PRs — and only one carries the packageRules keyed on
`matchManagers: ["custom.regex"]`. community.general 13.3.0 arrived exactly that way on 2026-08-20:
#257 through the grouped custom manager, and #204 through the built-in `ansible-galaxy` manager,
outside the manual-lockstep group and outside its deliberate `automerge: false`.

What let it hide for months is that the config asserted the opposite in prose — the custom
manager's own description said the built-in one "isn't reliably matching this path here". Renovate's
dependency dashboard listed `ansible/requirements.yml` under BOTH manager sections the whole time.
A claim in a description is not a constraint; this file is.

Run: uv run pytest scripts/tests/test_renovate_manager_overlap.py
"""

from __future__ import annotations

import json
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_CONFIG = json.loads((_REPO / "renovate.json").read_text())
_MANAGERS = _CONFIG["customManagers"]

# (built-in manager) -> the file its default patterns claim, which a custom manager already owns.
# Only managers this repo actually collides with; adding one means proving the collision first.
SHADOWING_BUILT_INS = {
    "ansible-galaxy": "ansible/requirements.yml",
}


def test_shadowing_built_in_managers_are_disabled() -> None:
    """A file owned by a custom manager must not also be scanned by an enabled built-in one."""
    for manager, path in SHADOWING_BUILT_INS.items():
        basename = path.rsplit("/", 1)[-1]
        # managerFilePatterns are regexes, so the literal basename is not a substring of
        # `requirements\.yml`. Dropping the escapes is enough to compare — these patterns are
        # plain paths with escaped dots, not general expressions.
        owned = [
            m
            for m in _MANAGERS
            if any(basename in p.replace("\\", "") for p in m["managerFilePatterns"])
        ]
        assert owned, (
            f"{path} is no longer owned by a custom manager — either re-enable the built-in "
            f"{manager} manager deliberately, or drop it from SHADOWING_BUILT_INS"
        )
        cfg = _CONFIG.get(manager)
        assert cfg is not None and cfg.get("enabled") is False, (
            f"the built-in {manager} manager also matches {path}, which a custom manager owns. "
            f"Leaving both enabled opens two PRs per bump, and the built-in copy sits outside "
            f'every packageRule keyed on matchManagers: ["custom.regex"] — including the '
            f"deliberate automerge: false. Disable it as a manager object: "
            f'"{manager}": {{"enabled": false}}'
        )


def test_enabled_managers_is_not_used_as_the_fix() -> None:
    """`enabledManagers` is an allowlist, and this repo needs four built-in managers.

    Reaching for it to silence the overlap above would disable `dockerfile`, `github-actions`,
    `pep621` and `pyenv` in one stroke — three of which have lockstep tests in
    test_renovate_managers.py that would then be guarding pins nothing updates.
    """
    assert "enabledManagers" not in _CONFIG, (
        "enabledManagers is an allowlist: adding it disables every built-in manager not named, "
        "including dockerfile, github-actions, pep621 and pyenv. Disable a specific manager with "
        'its own object instead — "<manager>": {"enabled": false}'
    )
