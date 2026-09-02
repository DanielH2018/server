"""The text a deferred change's alert prescribes, and the budget behind the forward-only arm.

A remediation that names the wrong playbook, a tag that matches nothing, or a consumer the
push did not touch is a green recap over an unapplied change -- `ansible-playbook` exits 0 on
a tag that selects no task. `deferred_service_alerts` is the combined-push remainder main()
reads on both branches; `broad_budget_ok` is the forward-only decision written as a predicate
so it fails instead of rotting.
"""

# ansible/roles/setup/gitops_deploy/tests/test_deploy_remediation.py

import pathlib

from deploy_changes import services_from_changed_paths, shared_module_consumers
from deploy_remediation import (
    broad_budget_ok,
    broad_remediation,
    deferred_service_alerts,
    k8s_remediation,
)

_K8S_ROLES_DIR = pathlib.Path(__file__).parents[3] / "k8s"


def test_broad_remediation_deploy_only_names_deploy_yml():
    cmd = broad_remediation(True, False)
    assert "ansible/deploy.yml" in cmd
    assert "initial_setup.yml" not in cmd


def test_broad_remediation_setup_only_names_initial_setup_not_deploy():
    # The M1 fix: a setup-plane broad change must NOT tell the operator to run deploy.yml (a no-op
    # for roles/setup/**) — it names initial_setup.yml --tags <role>.
    cmd = broad_remediation(False, True)
    assert "ansible/initial_setup.yml --tags <role>" in cmd
    assert "ansible/deploy.yml" not in cmd


def test_broad_remediation_both_planes_names_both():
    cmd = broad_remediation(True, True)
    assert "ansible/deploy.yml" in cmd
    assert "ansible/initial_setup.yml --tags <role>" in cmd


def test_broad_remediation_names_the_playbook_that_includes_the_role():
    """PR #702's failure: roles/setup/k3s is in k3s-bringup.yml, never initial_setup.yml."""
    cmd = broad_remediation(False, True, {"k3s"})
    assert "ansible/k3s-bringup.yml --tags k3s" in cmd
    assert "initial_setup.yml" not in cmd


def test_broad_remediation_uses_the_roles_real_tag():
    """`--tags chezmoi_setup` matches nothing; the playbook tags that role `chezmoi`."""
    cmd = broad_remediation(False, True, {"chezmoi_setup"})
    assert "ansible/initial_setup.yml --tags chezmoi" in cmd


def test_broad_remediation_for_a_role_no_playbook_includes_names_its_consumers():
    """setup/common is read by two roles on two hosts and applied by no playbook of its own."""
    cmd = broad_remediation(False, True, {"common"})
    assert "applied by no playbook of its own" in cmd
    assert "k3s-bringup.yml" in cmd
    assert "-e target=daniel-pi" in cmd


def test_broad_remediation_without_roles_keeps_the_generic_placeholder():
    """Callers with no path list are unchanged — the placeholder is still correct for them."""
    assert "ansible/initial_setup.yml --tags <role>" in broad_remediation(
        False, True, set()
    )


def test_broad_remediation_puts_the_ff_merge_before_the_playbook():
    """Ansible renders from the working tree, so a playbook run before the merge copies the
    PRE-merge files and recaps `changed=0` — a clean-looking run over the old code. An operator
    following the reverse order shipped the previous deploy_logic.py on 2026-09-01."""
    cmd = broad_remediation(False, True)
    assert cmd.index("git merge --ff-only") < cmd.index("ansible-playbook"), cmd


def test_broad_remediation_names_the_branch_it_is_given():
    """gitops_deploy.py reads BRANCH from config.env; a hardcoded `master` would print a
    command that does nothing on a host tracking anything else."""
    assert "origin/release" in broad_remediation(False, True, branch="release")
    assert "origin/master" in broad_remediation(False, True)


# review-M1: the deploy path used to evaluate the tasks/meta defer-and-alert ONLY inside
# `if not cs.services:`, so a COMBINED push (svcA's template + svcB's meta/tasks) deployed svcA
# and silently swallowed svcB's unapplied structural change. deferred_service_alerts(cs, deployed)
# is what main() now calls on BOTH branches; it returns the (tasks, meta) remainder that was NOT
# redeployed. deployed == cs.services on the deploy path, set() on the docs-only branch.
def test_deferred_alerts_combined_push_flags_other_services_meta():
    # svcA template + svcB meta: svcA deploys, but svcB's graph change is ff-merged with no
    # redeploy — it must still be flagged (the exact combined-push hole).
    cs = services_from_changed_paths(
        [
            "ansible/roles/containers/prometheus/templates/prometheus.yml.j2",
            "ansible/roles/containers/dozzle/meta/deps.yml",
        ]
    )
    assert cs.services == {"prometheus"}
    assert deferred_service_alerts(cs, cs.services) == (set(), {"dozzle"})


def test_deferred_alerts_combined_push_flags_other_services_tasks():
    # Same hole, tasks/ channel: svcA template deploys svcA, svcB's tasks change is left unapplied.
    cs = services_from_changed_paths(
        [
            "ansible/roles/containers/prometheus/templates/prometheus.yml.j2",
            "ansible/roles/containers/sonarr/tasks/main.yml",
        ]
    )
    assert deferred_service_alerts(cs, cs.services) == ({"sonarr"}, set())


def test_deferred_alerts_same_service_meta_rode_the_redeploy():
    # svcA template + svcA meta: svcA IS redeployed (scoped --tags reran its role / it's on the
    # graph), so its bundled meta change needs no alert — the remainder is empty.
    cs = services_from_changed_paths(
        [
            "ansible/roles/containers/dozzle/templates/docker-compose.yml.j2",
            "ansible/roles/containers/dozzle/meta/deps.yml",
        ]
    )
    assert deferred_service_alerts(cs, cs.services) == (set(), set())


def test_deferred_alerts_docs_only_branch_flags_full_sets():
    # The no-services branch passes deployed=set(): a meta-only (or tasks-only) push flags the
    # whole set, preserving the original defer-and-alert behavior.
    cs = services_from_changed_paths(["ansible/roles/containers/dozzle/meta/deps.yml"])
    assert deferred_service_alerts(cs, set()) == (set(), {"dozzle"})


def test_deferred_alerts_mixed_tasks_and_meta_remainders():
    # A three-way push: svcA deploys; svcB tasks and svcC meta are both left unapplied and flagged
    # on their respective channels.
    cs = services_from_changed_paths(
        [
            "ansible/roles/containers/prometheus/templates/prometheus.yml.j2",
            "ansible/roles/containers/sonarr/tasks/main.yml",
            "ansible/roles/containers/radarr/meta/deps.yml",
        ]
    )
    assert deferred_service_alerts(cs, cs.services) == ({"sonarr"}, {"radarr"})


def _prescribed_tags(msg: str) -> set[str]:
    """Every tag the remediation message actually tells an operator to pass to --tags."""
    out: set[str] = set()
    for chunk in msg.split("--tags ")[1:]:
        out.update(t for t in chunk.split("`")[0].strip().split(",") if t)
    return out


def test_k8s_remediation_never_prescribes_a_tag_that_deploys_nothing():
    """The alert must not name `--tags <role>` for a role with no containers_list entry.

    deploy.yml includes k8s roles per containers_list entry with tags: [<entry name>], so a tag
    matching no entry selects nothing and Ansible EXITS 0 — the operator runs the prescribed
    command, sees green, and the change is never applied. Eight roles are in that position and
    they are the shared plane (manifests is the apply+rollout path for every workload;
    volume-revert is the auto-deploy rollback path).

    Cross-checked against scripts/deploy_tools/deploy_tags.known_tags(), the same source ./scripts/deploy.sh
    validates against, so the alert and the wrapper cannot drift apart.
    """
    import sys

    sys.path.insert(
        0, str(pathlib.Path(__file__).resolve().parents[5] / "scripts" / "deploy_tools")
    )
    import deploy_tags

    declared = deploy_tags.known_tags()
    roles = {p.name for p in _K8S_ROLES_DIR.iterdir() if p.is_dir()}
    shared = roles - declared
    assert shared, (
        "expected some roles/k8s/ dirs to have no deploy tag; if this is now empty the "
        "remediation split is dead code and can be removed"
    )

    # An all-shared set must prescribe a full deploy and prescribe no tags at all. Asserting on
    # PRESCRIBED tags rather than the literal string "--tags", which also appears in the message's
    # own explanation of why a tag-scoped redeploy would not work.
    msg = k8s_remediation(shared, declared)
    assert _prescribed_tags(msg) == set(), (
        "k8s_remediation prescribed a --tags redeploy for roles with no containers_list "
        "entry: %s" % sorted(shared)
    )
    assert "`ansible-playbook ansible/deploy.yml`" in msg

    # A declared role still gets the cheap scoped form.
    one = sorted(roles & declared)[:1]
    if one:
        scoped = k8s_remediation(set(one), declared)
        assert "--tags %s" % one[0] in scoped

    # Every tag the mixed form prescribes must itself be deployable, and the shared roles must
    # still get the full-deploy instruction alongside.
    mixed = k8s_remediation(shared | set(one), declared)
    assert _prescribed_tags(mixed) <= declared, (
        "the mixed form prescribed undeployable tags: %s"
        % sorted(_prescribed_tags(mixed) - declared)
    )
    assert "`ansible-playbook ansible/deploy.yml`" in mixed


def test_a_shared_module_edit_names_every_consumer_role():
    """`_ACTIVE_K8S` maps a path to the role whose directory holds it, which is right for a
    manifest and wrong for a shared library. bridge_common.py lives under monitor-bridge and
    autofix-bridge imports it, so after the #407 split an edit there emitted
    `--tags monitor-bridge` alone and autofix-bridge's ConfigMap kept the old copy with
    nothing reporting it (2026-08-25 review M-2).
    """
    repo = pathlib.Path(__file__).resolve().parents[5]
    paths = ["ansible/roles/k8s/monitor-bridge/files/bridge_common.py"]
    consumers = shared_module_consumers(paths, repo)
    assert "autofix-bridge" in consumers, (
        "the deployer cannot see that autofix-bridge imports bridge_common, so a shared "
        "edit ff-merges leaving its ConfigMap stale: %s" % sorted(consumers)
    )
    assert "monitor-bridge" not in consumers, "the owning role is already in cs.k8s"

    declared = {"monitor-bridge", "autofix-bridge"}
    assert (
        _prescribed_tags(k8s_remediation({"monitor-bridge"}, declared, consumers))
        == declared
    )


def test_a_consumer_this_host_does_not_declare_is_not_escalated():
    """Intersect with `declared` BEFORE the union.

    A consumer absent from this host's containers_list has no deploy tag here, so folding it in raw
    would land it in `shared` and escalate a scoped `--tags` into "run a full deploy" -- for a role
    this host does not deploy at all.
    """
    declared = {"monitor-bridge"}
    msg = k8s_remediation({"monitor-bridge"}, declared, {"autofix-bridge"})
    assert _prescribed_tags(msg) == {"monitor-bridge"}
    assert "`ansible-playbook ansible/deploy.yml`" not in msg, (
        "an undeclared consumer escalated the instruction to a full deploy: %s" % msg
    )


# --- broad apply budget ------------------------------------------------------------------
#
# The reason the deploy-plane arm is forward-only, written as a predicate rather than as a
# comment. A comment rots silently when TimeoutStartSec or the measured deploy time moves;
# this fails.


def test_a_scoped_setup_run_fits_the_budget():
    """`initial_setup.yml --tags <role>` is small enough to fund a rollback re-run."""
    assert broad_budget_ok(forward_s=300, rollback_s=300, flock_s=180, timeout_s=2700)


def test_a_full_deploy_plus_rollback_is_flagged_over_budget():
    """Measured 2026-08-22: a full deploy plus rollback leaves 96s against TimeoutStartSec.

    A full deploy.yml is 1212s. 180 + 1212 + 1212 = 2604 against TimeoutStartSec=2700 leaves 96s, so
    a run four percent slower than measured is SIGTERMed mid-rollback -- which strands the tree at
    the failed commit with live state half-applied. This is the reject half, and it is the whole
    argument for forward-only.
    """
    assert not broad_budget_ok(
        forward_s=1212, rollback_s=1212, flock_s=180, timeout_s=2700
    )


def test_the_budget_predicate_tracks_the_units_real_timeout():
    """Pins the numbers the forward-only decision rests on.

    If TimeoutStartSec is raised in gitops-deploy.service.j2, this fails and the decision gets
    revisited deliberately rather than drifting.

    It fired as designed on 2026-08-29, when the staging gate's budgets raised the ceiling to 60min.
    Re-derived at that ceiling: 180 + 1212 + 1212 + 300 = 2904 against 3600 now FITS, so the budget
    is no longer what makes the deploy-plane arm forward-only. Nothing was armed by that —
    broad_budget_ok has no production caller; it is the reasoning made executable, and
    gitops_deploy.py's broad arm is forward-only in code either way. Funding a broad rollback is a
    deliberate change to make on its own evidence (a re-measured deploy.yml, and a decision about a
    rollback that can still be SIGTERMed), not a side effect of a ceiling raised for an unrelated
    feature.
    """
    unit = (
        pathlib.Path(__file__).resolve().parents[1]
        / "templates"
        / "gitops-deploy.service.j2"
    )
    assert "TimeoutStartSec=60min" in unit.read_text(), (
        "TimeoutStartSec moved — re-derive broad_budget_ok's verdict before trusting it"
    )
    assert broad_budget_ok(
        forward_s=1212, rollback_s=1212, flock_s=180, timeout_s=3600
    ), (
        "the re-derivation above says a broad rollback now fits at 60min; if this goes red the "
        "note in this docstring is stale and forward-only needs re-arguing from the budget again"
    )
