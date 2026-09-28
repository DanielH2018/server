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
        "image-builder",
        "longhorn-api",
        "manifests",
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


def test_the_include_role_form_resolves(callers):
    assert callers["arr-notification"] == {"radarr", "sonarr"}


def test_a_sibling_import_tasks_resolves_and_an_own_file_does_not(tmp_path):
    """`import_tasks: {{ role_path }}/../<x>/tasks/...` is a real edge; an import of the role's
    own task file is not. No live role uses the path form since #2813 folded game-stats-lib
    into game-stats, so a walk that lost it would otherwise go unnoticed."""
    roles = tmp_path / "ansible" / "roles" / "k8s"
    for role, body in {
        "lib": "- name: Stage\n  ansible.builtin.debug:\n",
        "game": (
            "- name: Stage the shared module\n"
            '  ansible.builtin.import_tasks: "{{ role_path }}/../lib/tasks/main.yml"\n'
            "- name: Stage its own half\n"
            "  ansible.builtin.import_tasks: own.yml\n"
        ),
    }.items():
        (roles / role / "tasks").mkdir(parents=True)
        (roles / role / "tasks" / "main.yml").write_text(body)
    assert role_callers(tmp_path) == {"lib": {"game"}}


def test_a_service_role_is_not_a_callee(callers):
    """The reject half: a walk matching any `k8s/` string would call sonarr a helper."""
    assert "sonarr" not in callers
    assert "jellyfin" not in callers


def test_the_helpers_are_still_undeclared():
    """The suppression only applies to roles with no tag of their own."""
    declared = set(k8s_entries())
    assert not (_HELPERS & declared), "a helper grew a containers_list entry"
    assert "sonarr" in declared, "the reject half: an empty lookup would pass"


def test_a_named_repo_is_walked_instead_of_this_checkout(tmp_path, callers):
    """`narrow_broad.narrow(cwd=X)` must read X's caller graph, not this file's checkout.

    Both halves in one test, because the fixture tree is what makes the reject half
    meaningful: the derived edge is the throwaway tree's, and none of the live tree's
    helpers may appear in it.
    """
    tasks = tmp_path / "ansible" / "roles" / "k8s" / "caller" / "tasks"
    tasks.mkdir(parents=True)
    (tasks / "main.yml").write_text("- include_role:\n    name: k8s/helper\n")

    derived = role_callers(tmp_path)

    assert derived == {"helper": {"caller"}}, derived
    assert callers, "the live graph is empty, so the reject half below proves nothing"
    assert not (set(derived) & set(callers)), derived
