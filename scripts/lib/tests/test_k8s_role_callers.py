#!/usr/bin/env python3
"""`role_callers` — which k8s roles reach another role's tasks, read from the live tree.

The consumer is `scripts/deploy_tools/land_tags.py`: a helper role whose callers were ALL
deployed is already applied, and reporting it as `needs-manual-apply` sends an operator at a
full `deploy.yml` for nothing (issue #1397). A census that silently returned an empty map
would suppress no note at all here, but the same map is what decides suppression — so the
non-vacuity half below names the roles it must find rather than counting them.

Run: uv run pytest scripts/lib/tests/test_k8s_role_callers.py
"""

import pytest

from lib.k8s_roles import k8s_entries, role_callers

# The helper roles: every role under ansible/roles/k8s/ that another role reaches and that has
# no containers_list entry of its own. Named, not counted -- a rename or a moved tasks file
# leaves a count-based assertion passing over a map that lost the member it was checking.
_HELPERS = frozenset(
    {
        "arr-notification",
        "cronjob-gate",
        "game-stats-lib",
        "image-builder",
        "longhorn-api",
        "manifests",
        "rollout-drain",
        "volume-claim",
        "volume-revert",
        "volume-snapshot",
    }
)


@pytest.fixture(scope="module")
def callers():
    return role_callers()


def test_every_named_helper_has_at_least_one_caller(callers):
    """Non-vacuity. A helper with no caller derived means a task form the walk misses."""
    missing = sorted(h for h in _HELPERS if not callers.get(h))
    assert not missing, f"no caller derived for {missing} -- the include form changed?"


def test_the_two_include_forms_both_resolve(callers):
    """`include_role: name: k8s/<x>` and a sibling `import_tasks` are both real edges.

    game-stats-lib is reached ONLY by `import_tasks: {{ role_path }}/../game-stats-lib/...`,
    so a walk reading include_role alone finds it uncalled and every change to it reports.
    """
    assert callers["arr-notification"] == {"radarr", "sonarr"}
    assert callers["game-stats-lib"] == {"terraria-stats", "valheim-stats"}


def test_a_service_role_is_not_a_callee(callers):
    """The reject half: a walk matching any `k8s/` string would call sonarr a helper."""
    assert "sonarr" not in callers
    assert "jellyfin" not in callers


def test_the_helpers_are_still_undeclared():
    """The suppression only applies to roles with no tag of their own."""
    declared = set(k8s_entries())
    assert not (_HELPERS & declared), "a helper grew a containers_list entry"
    assert "sonarr" in declared, "the reject half: an empty lookup would pass"
