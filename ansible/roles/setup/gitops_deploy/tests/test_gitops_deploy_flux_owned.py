"""The deployer skips a k8s tag whose containers_list entry sets `owner` (D2.2).

Flux applies such a tag. The tick fast-forwards its paths into `cs.flux` and logs them, and
never applies, defers, alerts on or owes it. The deploy plane drops it before its empty check,
so a narrowed list holding only such tags applies nothing. Ownership is read at the commit
being applied, not from the checkout. The plan is
docs/target-architecture/deploy/d2-first-leaf.md, *Steps, as PRs*, item 5.

Each behaviour is an `_is_clean` and `_is_flagged` pair: the Ansible-owned entry still takes
the Ansible path, and the same entry with `owner` does not.

Run: uv run pytest ansible/roles/setup/gitops_deploy/tests/test_gitops_deploy_flux_owned.py
"""

import dataclasses

from deployer_lib import deploy_k8s_owed
from deployer_lib import deploy_phases
from deployer_lib import deploy_tick_types
from deployer_lib.deploy_changes import ChangeSet
from _deploy_fakes import receipt_applied
from gitops_ledger import OWED_K8S_DEFERRED, OWED_K8S_UNAPPLIED

LOCAL = "1" * 40
ORIGIN = "2" * 40
HOST_VARS = "ansible/inventory/host_vars/test-host.yml"
GROUP_VARS = "ansible/inventory/group_vars/all.yml"
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
    """A range that bumps sonarr's image and nothing else, with auto-deploy armed.

    `diffs` is scripted only for a test that expects the promotion to read it. The fake
    asserts on an unscripted read, so a Flux-owned sonarr that reached the promotion fails
    there.
    """
    tick.config = dataclasses.replace(
        tick.config,
        k8s_autodeploy_enabled=True,
        k8s_autodeploy_denylist=frozenset(),
        k8s_autodeploy_pilot=frozenset(),
    )
    tick.paths = [SONARR_DEFAULTS]
    tick.tree_listing = SONARR_DEFAULTS + "\n"
    tick.files[f"{ORIGIN}:{SONARR_DEFAULTS}"] = (
        "sonarr_image: x:2\nk8s_autodeploy: true\n"
    )


def _plan(tick, state) -> deploy_tick_types.TickPlan:
    target = deploy_tick_types.TickTarget(
        local=LOCAL, origin=ORIGIN, hold=None, dirty=False, status="", action="deploy"
    )
    return deploy_phases.plan_tick(tick.tools, state, tick.config, target)


def _owed(state, cls: str = OWED_K8S_UNAPPLIED) -> list[str]:
    return sorted(e.service for e in state.owed_pending(cls))


# ── An image bump ──────────────────────────────────────────────────────────────────────────


def test_an_ansible_owned_image_bump_still_promotes_is_clean(
    gitops_deploy, tick, state
):
    _declare(tick, checkout={}, origin={})
    _sonarr_image_bump(tick)
    tick.diffs["sonarr"] = "--- a\n+++ b\n-sonarr_image: x:1\n+sonarr_image: x:2\n"
    plan = _plan(tick, state)
    assert plan.cs.k8s_deploy == {"sonarr"}
    assert plan.cs.flux == set()


def test_a_flux_owned_image_bump_lands_in_flux_is_flagged(gitops_deploy, tick, state):
    _declare(tick, checkout={"sonarr": "flux"}, origin={"sonarr": "flux"})
    _sonarr_image_bump(tick)
    plan = _plan(tick, state)
    assert plan.cs.flux == {"sonarr"}
    assert plan.cs.k8s_deploy == set()
    assert plan.cs.k8s == set()
    assert "sonarr" not in plan.k8s_services


def test_a_flux_owned_image_bump_merges_and_does_nothing_else_is_flagged(
    gitops_deploy, tick, state, capsys
):
    _declare(tick, checkout={"sonarr": "released"}, origin={"sonarr": "released"})
    _sonarr_image_bump(tick)
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert tick.merges == [ORIGIN]
    assert tick.playbooks == []
    assert tick.posts == []
    assert _owed(state) == []
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
    tick.diffs["sonarr"] = "--- a\n+++ b\n-sonarr_image: x:1\n+sonarr_image: x:2\n"
    plan = _plan(tick, state)
    assert plan.cs.k8s_deploy == {"sonarr"}
    assert plan.owners == {}


def test_an_unreadable_origin_reads_ownership_from_the_checkout(
    gitops_deploy, tick, state, capsys
):
    tick.declare(_host_vars({"sonarr": "flux"}))
    _sonarr_image_bump(tick)
    plan = _plan(tick, state)
    assert plan.cs.flux == {"sonarr"}
    assert "reading ownership from the checkout" in capsys.readouterr().out


# ── The deploy plane ───────────────────────────────────────────────────────────────────────


def test_a_narrow_output_of_only_a_flux_owned_tag_applies_nothing_is_flagged(
    gitops_deploy, tick, state, capsys
):
    _declare(tick, checkout={}, origin={"bento-pdf": "flux"})
    tick.paths = [GROUP_VARS]
    tick.narrow = (0, "bento-pdf")
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert tick.playbooks == []
    assert tick.merges == [ORIGIN]
    assert receipt_applied(state) == {"ansible/deploy.yml": ("narrowed-to-nothing",)}
    assert _owed(state) == []
    out = capsys.readouterr().out
    assert "narrow: dropping bento-pdf: owned by flux" in out
    assert "the range moves no rendered output" in out


def test_a_narrow_output_with_an_ansible_owned_tag_applies_that_tag_alone_is_clean(
    gitops_deploy, tick, state
):
    _declare(tick, checkout={}, origin={"bento-pdf": "flux"})
    tick.paths = [GROUP_VARS]
    tick.narrow = (0, "bento-pdf,radarr")
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert [argv[-3:] for argv in tick.playbooks] == [
        ["ansible/deploy.yml", "--tags", "radarr"]
    ]
    assert receipt_applied(state) == {"ansible/deploy.yml": ("radarr",)}


def test_a_refusal_naming_no_tags_owes_every_ansible_owned_service(
    gitops_deploy, tick, state
):
    """An empty refusal owes every declared service, and a Flux-owned one is not declared."""
    _declare(tick, checkout={}, origin={"bento-pdf": "flux", "sonarr": "released"})
    tick.paths = [GROUP_VARS]
    tick.narrow = (3, "")
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert tick.playbooks == []
    assert _owed(state) == ["radarr"]


# ── A ledger line written before the range that set `owner` ────────────────────────────────


def _owners_plan(owners: dict[str, str]) -> deploy_tick_types.TickPlan:
    return deploy_tick_types.TickPlan(
        cs=ChangeSet(), paths=[], k8s_services=set(), owners=owners
    )


def test_a_line_for_an_ansible_owned_service_is_kept_is_clean(state):
    state.record_owed(OWED_K8S_UNAPPLIED, ORIGIN, {"bento-pdf", "radarr"}, 1000.0)
    assert (
        deploy_k8s_owed.skip_not_ansible_owned(state, _owners_plan({}), ORIGIN) == set()
    )
    assert _owed(state) == ["bento-pdf", "radarr"]


def test_a_line_for_a_flux_owned_service_is_dropped_is_flagged(state, capsys):
    state.record_owed(OWED_K8S_UNAPPLIED, ORIGIN, {"bento-pdf", "radarr"}, 1000.0)
    state.record_owed(OWED_K8S_DEFERRED, ORIGIN, {"sonarr"}, 1000.0)
    plan = _owners_plan({"bento-pdf": "flux", "sonarr": "released"})
    assert deploy_k8s_owed.skip_not_ansible_owned(state, plan, ORIGIN) == {
        "bento-pdf",
        "sonarr",
    }
    assert _owed(state) == ["radarr"]
    assert _owed(state, OWED_K8S_DEFERRED) == []
    assert "flux: bento-pdf: dropped its k8s_unapplied line" in capsys.readouterr().out


def test_a_tick_drops_a_line_written_before_the_flip_is_flagged(
    gitops_deploy, tick, state
):
    """D2.7's own tick: the line predates the range that sets `owner`."""
    _declare(tick, checkout={}, origin={"bento-pdf": "flux"})
    state.record_owed(OWED_K8S_UNAPPLIED, LOCAL, {"bento-pdf"}, 1000.0)
    tick.paths = [GROUP_VARS]
    tick.narrow = (0, "bento-pdf")
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert _owed(state) == []
