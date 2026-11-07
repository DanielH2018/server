"""Every k8s role path routes to `cs.flux`, with no read of the role's `owner` (D6.4).

`promote` and Flux apply every k8s role, so the tick fast-forwards its paths and logs them, and
never applies, defers, alerts on or owes them. Without the k8s branch of
`services_from_changed_paths`, such a path would land in no set of the ChangeSet.

Run: uv run pytest ansible/roles/setup/gitops_deploy/tests/test_gitops_deploy_flux_owned.py
"""

from deployer_lib import deploy_phases, deploy_tick_types
from deployer_lib.deploy_changes import ChangeSet, services_from_changed_paths
from lib.repo_paths import REPO

LOCAL = "1" * 40
ORIGIN = "2" * 40
SONARR_DEFAULTS = "ansible/roles/k8s/sonarr/defaults/main.yml"


def _plan(tick, state) -> deploy_tick_types.TickPlan:
    target = deploy_tick_types.TickTarget(
        local=LOCAL, origin=ORIGIN, hold=None, dirty=False, status="", action="deploy"
    )
    return deploy_phases.plan_tick(tick.tools, state, tick.config, target)


def _sonarr_image_bump(tick) -> None:
    """A range that bumps sonarr's image and nothing else."""
    tick.paths = [SONARR_DEFAULTS]
    tick.files[f"{ORIGIN}:{SONARR_DEFAULTS}"] = "sonarr_image: x:2\n"


def test_every_k8s_role_path_lands_in_flux_and_no_other_set_is_flagged():
    roles = {
        p.name
        for p in (REPO / "ansible/roles/k8s").iterdir()
        if (p / "defaults").is_dir()
    }
    assert "bento-pdf" in roles and len(roles) >= 50
    for role in sorted(roles):
        path = f"ansible/roles/k8s/{role}/templates/x.yaml.j2"
        assert services_from_changed_paths([path]) == ChangeSet(flux={role}), path


def test_a_path_outside_the_k8s_roles_stays_out_of_flux_is_clean():
    for path in (
        "ansible/roles/setup/gitops_deploy/tasks/main.yml",
        "ansible/inventory/group_vars/all.yml",
        "scripts/deploy_tools/land.py",
        "docs/landing.md",
    ):
        assert services_from_changed_paths([path]).flux == set(), path


def test_the_route_reads_no_owner_value(gitops_deploy, tick, state):
    """A misspelt `owner` raises in the readers that remain, and the tick reads none of them."""
    tick.declare(
        "containers_list:\n  - name: sonarr\n    platform: k8s\n    owner: flx\n"
    )
    _sonarr_image_bump(tick)
    assert _plan(tick, state).cs == ChangeSet(flux={"sonarr"})


def test_a_k8s_range_merges_and_does_nothing_else(gitops_deploy, tick, state, capsys):
    _sonarr_image_bump(tick)
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert tick.merges == [ORIGIN]
    assert tick.playbooks == []
    assert tick.posts == []
    assert f"flux: sonarr: fast-forwarding {SONARR_DEFAULTS}" in capsys.readouterr().out
