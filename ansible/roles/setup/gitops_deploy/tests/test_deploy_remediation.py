"""The text a deferred change's alert prescribes, and the budget behind the forward-only arm.

A remediation that names the wrong playbook, a tag that matches nothing, or a consumer the
push did not touch is a green recap over an unapplied change -- `ansible-playbook` exits 0 on
a tag that selects no task. `deferred_service_alerts` is the combined-push remainder main()
reads on both branches; `broad_budget_ok` is the forward-only decision written as a predicate
so it fails instead of rotting.
"""

# ansible/roles/setup/gitops_deploy/tests/test_deploy_remediation.py

import pathlib

import deploy_remediation

from deploy_changes import services_from_changed_paths, shared_module_consumers
from deploy_remediation import (
    broad_budget_ok,
    broad_remediation,
    deferred_service_alerts,
    k8s_remediation,
    manual_plane_remediation,
    maximal_tag_warning,
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
    manifest and wrong for a shared library. bridge/common.py lives under monitor-bridge and
    autofix-bridge imports it, so after the #407 split an edit there emitted
    `--tags monitor-bridge` alone and autofix-bridge's ConfigMap kept the old copy with
    nothing reporting it (2026-08-25 review M-2).
    """
    repo = pathlib.Path(__file__).resolve().parents[5]
    paths = ["ansible/roles/k8s/monitor-bridge/files/bridge/common.py"]
    consumers = shared_module_consumers(paths, repo)
    assert "autofix-bridge" in consumers, (
        "the deployer cannot see that autofix-bridge imports bridge.common, so a shared "
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


def _k8s_tree(tmp_path, files):
    """A fake repo holding `ansible/roles/k8s/<role>/files/<path>` -> source pairs."""
    for rel, source in files.items():
        p = tmp_path / "ansible" / "roles" / "k8s" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(source)
    return tmp_path


def test_a_shared_module_inside_a_package_is_still_seen(tmp_path):
    """The detector sees `files/bridge/common.py`, in every spelling a consumer can use.

    The first version matched one level (`files/<name>.py`) and a bare `import bridge.common`.
    Moving the shared module into a package would have made it invisible to the one check that
    exists for it, and the deployer would have gone back to emitting `--tags monitor-bridge`
    alone -- the M-2 silence, reintroduced by a rename. The live-tree test above cannot be this
    red-proof while the tree is flat; this one exercises depth on both sides, the owner's and
    the consumer's.
    """
    repo = _k8s_tree(
        tmp_path,
        {
            "alpha/files/bridge/common.py": "def log(*a): pass\n",
            "beta/files/autofix.py": "from bridge import common\ncommon.log()\n",
            "gamma/files/sub/deep.py": "import bridge.common as bc\nbc.log()\n",
            "delta/files/y.py": "from bridge.common import log\nlog()\n",
            "theta/files/t.py": "from bridge import config, common as c\nc.log()\n",
            # Mentions only: a comment, a module sharing the prefix, a string. None of these
            # bind the module, so none may count.
            "epsilon/files/z.py": (
                "# from bridge import common\n"
                "import bridge.commonality\n"
                "x = 'from bridge import common'\n"
            ),
        },
    )
    consumers = shared_module_consumers(
        ["ansible/roles/k8s/alpha/files/bridge/common.py"], repo
    )
    assert consumers == {"beta", "gamma", "delta", "theta"}, sorted(consumers)


def test_a_flat_shared_module_matches_on_its_whole_name(tmp_path):
    """`import flat_lib_extra` is not an import of `flat_lib`."""
    repo = _k8s_tree(
        tmp_path,
        {
            "alpha/files/flat_lib.py": "X = 1\n",
            "zeta/files/w.py": "import flat_lib\n",
            "eta/files/v.py": "import flat_lib_extra\nfrom flat_lib_extra import X\n",
        },
    )
    consumers = shared_module_consumers(
        ["ansible/roles/k8s/alpha/files/flat_lib.py"], repo
    )
    assert consumers == {"zeta"}, sorted(consumers)


def test_the_owning_role_is_never_reported_as_its_own_consumer(tmp_path):
    repo = _k8s_tree(
        tmp_path,
        {
            "alpha/files/bridge/common.py": "X = 1\n",
            "alpha/files/check.py": "from bridge import common\n",
        },
    )
    assert (
        shared_module_consumers(
            ["ansible/roles/k8s/alpha/files/bridge/common.py"], repo
        )
        == set()
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


# ── #2294: the role tag is the maximal apply, and the printed command says so ──────────────
# A three-line RBAC addition to `k3s_readonly_crd_api_groups` was answered with
# `ansible-playbook ansible/k3s-bringup.yml --tags k3s` on 2026-09-22. That command restarts
# k3s and re-encrypts etcd; the change needed `--tags kubeconfig` (ok=15 changed=2).


def test_the_k3s_role_tag_carries_what_running_it_does():
    """The command is still the role tag, now carrying a warning.

    No assertion on the warning's prose: asserting a sentence copied out of the module that
    defines it proves only that the file equals itself. What the warning CLAIMS is held up by
    the two oracles below, against the role's own
    task files.
    """
    cmd = broad_remediation(False, True, {"k3s"})
    assert "ansible/k3s-bringup.yml --tags k3s" in cmd
    assert "WARNING" in cmd


def test_a_role_whose_tag_is_not_maximal_carries_no_warning():
    """The rejecting half: the warning must not ride every setup command."""
    assert "WARNING" not in broad_remediation(False, True, {"chezmoi_setup"})
    assert maximal_tag_warning("chezmoi_setup") == ""


def test_the_manual_plane_remediation_carries_the_warning_too():
    """The journal line, the Discord alert and land.sh all quote one composer."""
    cmd = manual_plane_remediation({"k3s"})
    assert "WARNING" in cmd
    assert "--tags kubeconfig" not in cmd.split("WARNING")[0]


# ── #2307: a derived narrow tag replaces the role tag, and the warning with it ──────────────


def test_a_narrowed_role_prints_its_own_tags_and_drops_the_warning():
    """The narrowing's whole point, on both composers.

    The warning describes what `--tags k3s` does. Printed beside `--tags kubeconfig` it would
    warn about a run the operator is not being told to make, which is how a real warning gets
    read as boilerplate.
    """
    narrow = {"k3s": frozenset({"kubeconfig"})}
    for cmd in (
        broad_remediation(False, True, {"k3s"}, narrow_tags=narrow),
        manual_plane_remediation({"k3s"}, narrow),
    ):
        assert "ansible/k3s-bringup.yml --tags kubeconfig" in cmd
        assert "WARNING" not in cmd


def test_a_role_the_derivation_refused_keeps_the_role_tag_and_the_warning():
    """The rejecting half: an empty tag set is a refusal, not an empty `--tags` value.

    `--tags` with nothing after it runs the WHOLE playbook, so a refusal that leaked through
    as an empty string would prescribe every setup role on the host.
    """
    for narrow in ({}, {"k3s": frozenset()}):
        cmd = manual_plane_remediation({"k3s"}, narrow)
        assert "ansible/k3s-bringup.yml --tags k3s" in cmd
        assert "WARNING" in cmd


def test_several_narrow_tags_are_one_comma_joined_tags_value():
    """Two ranges can make one role pending, and both tags have to run."""
    narrow = {"k3s": frozenset({"kubeconfig", "coredns"})}
    assert "--tags coredns,kubeconfig" in manual_plane_remediation({"k3s"}, narrow)


_K3S_TASKS = pathlib.Path(__file__).parents[2] / "k3s" / "tasks"


def test_every_narrower_tag_the_warning_names_exists_in_the_role():
    """The warning points at real tags, or it sends an operator after a no-op.

    `--tags <nothing>` makes Ansible exit 0 having run no task, which is the silent-success
    failure `setup_tags_for` and `k8s_remediation` both exist to avoid. The tag list is a
    module constant rather than prose this test re-parses, so a tag renamed in `tasks/`
    breaks this and a reworded warning does not.
    """
    import re

    task_files = sorted(_K3S_TASKS.glob("*.yml"))
    assert len(task_files) >= 10, f"only {len(task_files)} k3s task files found"
    declared = set()
    for path in task_files:
        for block in re.findall(r"tags:\s*\[([^\]]*)\]", path.read_text()):
            declared.update(x.strip() for x in block.split(",") if x.strip())
    assert "kubeconfig" in declared, (
        "the k3s role no longer declares a `kubeconfig` tag"
    )

    offered = set(deploy_remediation._K3S_NARROWER_TAGS)
    assert offered, "the k3s warning offers no narrower tag at all"
    assert offered <= declared, (
        f"the k3s warning offers tags the role does not declare: {sorted(offered - declared)}"
    )
    for tag in offered:
        assert f"`{tag}`" in maximal_tag_warning("k3s"), (
            f"{tag} dropped out of the warning"
        )


def test_every_gate_the_warning_names_is_read_by_the_role():
    """The warning says the control-plane tasks are GATED; the gates must still be there.

    Asserting the identifiers against `tasks/server.yml` rather than the warning's sentence:
    a gate removed there makes the warning understate what `--tags k3s` does, which is the
    direction that gets an operator hurt. The claims come from reading that file, not from
    issue #2294's body, which described all three effects as unconditional.
    """
    server = (_K3S_TASKS / "server.yml").read_text()
    assert "secrets-encrypt rotate-keys" in server
    for gate in deploy_remediation._K3S_CONTROL_PLANE_GATES:
        assert gate in server, f"`{gate}` is no longer read by tasks/server.yml"
        assert f"`{gate}`" in maximal_tag_warning("k3s"), (
            f"{gate} dropped out of the warning"
        )
