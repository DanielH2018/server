"""A broad tick that parks says WHY, in the journal, on every tick.

Split out of test_gitops_deploy_main_branches.py, which is at its module-length cap. The pair
here is one input the park log must fire on and one it must stay silent on — a guard that
logged on every branch and one that logged on none read the same from the passing side alone.
"""

# ansible/roles/setup/gitops_deploy/tests/test_gitops_deploy_broad_park.py

# The SHA the `tick` fixture fast-forwards to; see test_gitops_deploy_main_branches.py for why
# `from conftest import` is avoided.
ORIGIN = "2" * 40


# ── a park says why, in the journal, on EVERY tick (#1467) ─────────────────────────────
K3S_SETUP = "ansible/roles/setup/k3s/tasks/longhorn-backup.yml"


def test_an_unresolvable_setup_role_names_its_reason_on_every_tick(
    gitops_deploy, tick, capsys
):
    """The 2026-09-09 park: `roles/setup/k3s/` is applied by k3s-bringup.yml, so no tag resolves.

    The Discord page is throttled once per SHA. Before this the journal was throttled with it,
    so nine commits sat unmerged for twenty minutes with no per-tick line naming a reason.
    """
    tick.paths = [K3S_SETUP]
    assert gitops_deploy.main(tick.tools) == 0
    assert gitops_deploy.main(tick.tools) == 0, "the range is unchanged; it re-evals"
    out = capsys.readouterr().out
    assert out.count("parked, nothing merged") == 2, "the journal is not throttled"
    assert "k3s" in out and "k3s-bringup.yml" in out
    assert tick.merges == [] and tick.playbooks == []
    assert len(tick.posts) == 1, "the PAGE is still once per SHA"


def test_a_setup_role_the_deployer_can_apply_logs_no_park(gitops_deploy, tick, capsys):
    """The rejecting half: a resolvable setup role merges and applies, and says nothing."""
    tick.paths = ["ansible/roles/setup/gitops_deploy/tasks/main.yml"]
    assert gitops_deploy.main(tick.tools) == 0
    assert "parked, nothing merged" not in capsys.readouterr().out
    assert tick.merges == [ORIGIN] and tick.playbooks
