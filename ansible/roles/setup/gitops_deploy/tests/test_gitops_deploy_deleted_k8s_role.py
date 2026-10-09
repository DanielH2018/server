"""A k8s role the range deletes owes nothing, so the tick records no `k8s_unapplied` line.

Retiring `k8s/volume-claim` (PR #3567) left a line no deploy could discharge: the role had no
callers, so no tag's release record could ever carry it (#3568). `plan_tick` reads the role
directories at origin and drops a changed role that is gone there. An empty or unreadable
listing drops nothing, because a kept line costs one `clear-owed` and a dropped one loses the
only record of a change.

A line an earlier tick wrote, before the range that deleted the role, is dropped by the
discharge on the next tick, which asks the same question of `HEAD` (#3569).

Run: uv run pytest ansible/roles/setup/gitops_deploy/tests/test_gitops_deploy_deleted_k8s_role.py
"""

import dataclasses

import pytest

import deploy_phases
import deploy_tick_types
from gitops_ledger import OWED_K8S_UNAPPLIED

LOCAL = "1" * 40
ORIGIN = "2" * 40
DELETED_ROLE = "ansible/roles/k8s/volume-claim/tasks/main.yml"
SONARR_TEMPLATE = "ansible/roles/k8s/sonarr/templates/deployment.yaml.j2"
TARGET = deploy_tick_types.TickTarget(
    local=LOCAL, origin=ORIGIN, hold=None, dirty=False, status="", action="deploy"
)


@pytest.mark.parametrize(
    ("listing", "expected"),
    [
        ("ansible/roles/k8s/manifests\nansible/roles/k8s/sonarr\n", {"sonarr"}),
        # The red half: the same paths with the role still listed keep it.
        (
            "ansible/roles/k8s/sonarr\nansible/roles/k8s/volume-claim\n",
            {"sonarr", "volume-claim"},
        ),
        ("", {"sonarr", "volume-claim"}),
    ],
)
def test_plan_tick_drops_a_k8s_role_whose_directory_is_gone_at_origin(
    gitops_deploy, tick, settings, listing, expected, state
):
    tick.paths = [SONARR_TEMPLATE, DELETED_ROLE]
    tick.tree_listing = listing
    plan = deploy_phases.plan_tick(tick.tools, state, settings, TARGET)
    assert plan.cs.k8s == expected
    assert set(plan.cs.k8s_origins) >= expected


def test_plan_tick_drops_no_role_when_the_listing_cannot_be_read(
    gitops_deploy, tick, settings, capsys, state
):
    def run(argv, **kwargs):
        if argv[:2] == ["git", "ls-tree"]:
            raise RuntimeError("git ls-tree -> 128")
        return tick.run(argv, **kwargs)

    tick.paths = [SONARR_TEMPLATE, DELETED_ROLE]
    tick.tree_listing = "ansible/roles/k8s/sonarr\n"
    plan = deploy_phases.plan_tick(
        dataclasses.replace(tick.tools, run=run), state, settings, TARGET
    )
    assert plan.cs.k8s == {"sonarr", "volume-claim"}
    assert "could not list the k8s roles" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("listing", "owed"),
    [
        ("ansible/roles/k8s/manifests\n", []),
        (
            "ansible/roles/k8s/manifests\nansible/roles/k8s/volume-claim\n",
            ["volume-claim"],
        ),
    ],
)
def test_a_tick_deleting_a_shared_role_leaves_no_k8s_unapplied_line(
    gitops_deploy, tick, settings, listing, owed, state
):
    tick.paths = [DELETED_ROLE]
    tick.tree_listing = listing
    assert gitops_deploy.main(tick.tools, settings, state) == 0
    pending = state.owed_pending(OWED_K8S_UNAPPLIED)
    assert [e.service for e in pending] == owed


@pytest.mark.parametrize(
    ("listing", "owed"),
    [
        ("ansible/roles/k8s/manifests\n", []),
        # The red half: the role is still on disk, has no caller to carry it, and stays.
        (
            "ansible/roles/k8s/manifests\nansible/roles/k8s/volume-claim\n",
            ["volume-claim"],
        ),
        ("", ["volume-claim"]),
    ],
)
def test_a_pending_line_for_a_role_deleted_since_is_dropped_on_the_next_tick(
    gitops_deploy, tick, settings, listing, owed, state
):
    state.record_owed(OWED_K8S_UNAPPLIED, LOCAL, {"volume-claim"}, 1.0)
    tick.paths = []
    tick.tree_listing = listing
    assert gitops_deploy.main(tick.tools, settings, state) == 0
    pending = state.owed_pending(OWED_K8S_UNAPPLIED)
    assert [e.service for e in pending] == owed
