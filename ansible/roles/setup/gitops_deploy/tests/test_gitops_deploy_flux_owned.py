"""The deployer skips a k8s tag whose containers_list entry sets `owner` (D2.2).

Flux applies such a tag. The tick fast-forwards its paths into `cs.flux` and logs them, and
never applies, defers, alerts on or owes it. Ownership is read at the commit being applied,
not from the checkout. The plan is docs/target-architecture/deploy/d2-first-leaf.md, *Steps,
as PRs*, item 5. D6.12 deleted the image-bump promotion these tests also covered, so what is
left is the `cs.flux` routing, which D6.4 rewrites.

Each behaviour is an `_is_clean` and `_is_flagged` pair: the Ansible-owned entry stays in
`cs.k8s`, and the same entry with `owner` moves to `cs.flux`.

Run: uv run pytest ansible/roles/setup/gitops_deploy/tests/test_gitops_deploy_flux_owned.py
"""

from deployer_lib import deploy_phases
from deployer_lib import deploy_tick_types

LOCAL = "1" * 40
ORIGIN = "2" * 40
HOST_VARS = "ansible/inventory/host_vars/test-host.yml"
SONARR_DEFAULTS = "ansible/roles/k8s/sonarr/defaults/main.yml"


def _host_vars(owners: dict[str, str]) -> str:
    """A containers_list of bento-pdf, radarr and sonarr, each setting the owner given."""
    lines = ["containers_list:"]
    for name in ("bento-pdf", "radarr", "sonarr"):
        lines += [f"  - name: {name}", "    platform: k8s"]
        if name in owners:
            lines.append(f"    owner: {owners[name]}")
    return "\n".join(lines) + "\n"


def _declare(tick, *, checkout: dict[str, str], origin: dict[str, str]) -> None:
    """Script the host_vars in the checkout and at origin, with the owners each sets."""
    tick.declare(_host_vars(checkout))
    tick.files[f"{ORIGIN}:{HOST_VARS}"] = _host_vars(origin)


def _sonarr_image_bump(tick) -> None:
    """A range that bumps sonarr's image and nothing else."""
    tick.paths = [SONARR_DEFAULTS]
    tick.files[f"{ORIGIN}:{SONARR_DEFAULTS}"] = "sonarr_image: x:2\n"


def _plan(tick, state) -> deploy_tick_types.TickPlan:
    target = deploy_tick_types.TickTarget(
        local=LOCAL, origin=ORIGIN, hold=None, dirty=False, status="", action="deploy"
    )
    return deploy_phases.plan_tick(tick.tools, state, tick.config, target)


# ── An image bump ──────────────────────────────────────────────────────────────────────────


def test_an_ansible_owned_image_bump_stays_in_k8s_is_clean(gitops_deploy, tick, state):
    _declare(tick, checkout={}, origin={})
    _sonarr_image_bump(tick)
    plan = _plan(tick, state)
    assert plan.cs.k8s == {"sonarr"}
    assert plan.cs.flux == set()


def test_a_flux_owned_image_bump_lands_in_flux_is_flagged(gitops_deploy, tick, state):
    _declare(tick, checkout={"sonarr": "flux"}, origin={"sonarr": "flux"})
    _sonarr_image_bump(tick)
    plan = _plan(tick, state)
    assert plan.cs.flux == {"sonarr"}
    assert plan.cs.k8s == set()


def test_a_flux_owned_image_bump_merges_and_does_nothing_else_is_flagged(
    gitops_deploy, tick, state, capsys
):
    _declare(tick, checkout={"sonarr": "released"}, origin={"sonarr": "released"})
    _sonarr_image_bump(tick)
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert tick.merges == [ORIGIN]
    assert tick.playbooks == []
    assert tick.posts == []
    assert (
        f"flux: sonarr: owned by released at {ORIGIN[:8]} — fast-forwarding "
        f"{SONARR_DEFAULTS}" in capsys.readouterr().out
    )


# ── Ownership is read at the commit being applied ──────────────────────────────────────────


def test_an_owner_added_in_the_range_is_read_at_origin_is_flagged(
    gitops_deploy, tick, state
):
    """D2.7's own range: the checkout has no `owner` yet, and origin adds it."""
    _declare(tick, checkout={}, origin={"sonarr": "flux"})
    _sonarr_image_bump(tick)
    plan = _plan(tick, state)
    assert plan.cs.flux == {"sonarr"}
    assert plan.owners == {"sonarr": "flux"}


def test_an_owner_removed_in_the_range_is_read_at_origin_is_clean(
    gitops_deploy, tick, state
):
    """A handback's second PR: the checkout still has `owner`, and origin removes it."""
    _declare(tick, checkout={"sonarr": "released"}, origin={})
    _sonarr_image_bump(tick)
    plan = _plan(tick, state)
    assert plan.cs.k8s == {"sonarr"}
    assert plan.owners == {}


def test_an_unreadable_origin_reads_ownership_from_the_checkout(
    gitops_deploy, tick, state, capsys
):
    tick.declare(_host_vars({"sonarr": "flux"}))
    _sonarr_image_bump(tick)
    plan = _plan(tick, state)
    assert plan.cs.flux == {"sonarr"}
    assert "reading ownership from the checkout" in capsys.readouterr().out
