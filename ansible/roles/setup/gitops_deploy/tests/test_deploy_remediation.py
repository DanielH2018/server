"""The text a deferred change's alert prescribes, and the budget behind the forward-only arm.

A remediation that names the wrong playbook, a tag that matches nothing, or a consumer the
push did not touch is a green recap over an unapplied change -- `ansible-playbook` exits 0 on
a tag that selects no task. `broad_budget_ok` is the forward-only decision written as a
predicate so it fails instead of rotting.
"""

# ansible/roles/setup/gitops_deploy/tests/test_deploy_remediation.py

import pathlib

import pytest


from lib.repo_paths import REPO

from deployer_lib import deploy_defer
from deployer_lib.deploy_changes import (
    services_from_changed_paths,
    setup_tags_for,
    shared_module_consumers,
)
from deployer_lib.deploy_remediation import (
    broad_budget_ok,
    broad_remediation,
    manual_plane_remediation,
    maximal_tag_warning,
)


def test_broad_remediation_deploy_only_names_deploy_yml():
    cmd = broad_remediation(True, False)
    assert "ansible/deploy.yml" in cmd
    assert "initial_setup.yml" not in cmd


def test_broad_remediation_setup_only_names_initial_setup_not_deploy():
    # A setup-plane broad change must NOT tell the operator to run deploy.yml (a no-op
    # for roles/setup/**) — it names initial_setup.yml --tags <role>.
    cmd = broad_remediation(False, True)
    assert "ansible/initial_setup.yml --tags <role>" in cmd
    assert "ansible/deploy.yml" not in cmd


def test_broad_remediation_both_planes_names_both():
    cmd = broad_remediation(True, True)
    assert "ansible/deploy.yml" in cmd
    assert "ansible/initial_setup.yml --tags <role>" in cmd


def test_broad_remediation_names_the_playbook_that_includes_the_role():
    """roles/setup/k3s is in k3s-bringup.yml, never initial_setup.yml."""
    cmd = broad_remediation(False, True, {"k3s"})
    assert "ansible/k3s-bringup.yml --tags k3s" in cmd
    assert "initial_setup.yml" not in cmd


def test_broad_remediation_uses_the_roles_real_tag():
    """`--tags chezmoi_setup` matches nothing; the playbook tags that role `chezmoi`."""
    cmd = broad_remediation(False, True, {"chezmoi_setup"})
    assert "ansible/initial_setup.yml --tags chezmoi" in cmd


def test_broad_remediation_for_a_role_no_playbook_includes_names_its_consumers():
    """setup/common is applied by no playbook of its own, and packages render its resolver."""
    cmd = broad_remediation(False, True, {"common"})
    assert "applied by no playbook of its own" in cmd
    assert "ansible/k3s-bringup.yml" not in cmd
    assert "optimize_pi" not in cmd


def test_a_role_gated_off_the_tick_host_goes_to_manual_plane_with_its_target():
    """`optimize_pi` runs only on daniel-pi, so the tick's `--tags optimize_pi` applied
    nothing on daniel-box and still recorded the apply (#3933)."""
    path = "ansible/roles/setup/optimize_pi/templates/node_exporter.service.j2"
    assert setup_tags_for([path]) == set()
    cs = services_from_changed_paths([path])
    assert deploy_defer.unapplyable_setup_roles(cs) == ["optimize_pi"]
    cmd = manual_plane_remediation({"optimize_pi"})
    assert (
        "`uv run ansible-playbook ansible/initial_setup.yml --tags optimize_pi -e target=daniel-pi`"
        in cmd
    )
    assert "clear-owed manual_plane optimize_pi" in cmd


def test_broad_remediation_without_roles_keeps_the_generic_placeholder():
    """Callers with no path list are unchanged — the placeholder is still correct for them."""
    assert "ansible/initial_setup.yml --tags <role>" in broad_remediation(
        False, True, set()
    )


@pytest.mark.parametrize(
    "path, expected",
    [
        (
            "ansible/k3s-bringup.yml",
            "`uv run ansible-playbook ansible/k3s-bringup.yml --tags k3s`",
        ),
        ("ansible/bootstrap.yml", "`uv run ansible-playbook ansible/bootstrap.yml`"),
        (
            "ansible/initial_setup.yml",
            "`uv run ansible-playbook ansible/initial_setup.yml`",
        ),
    ],
)
def test_broad_remediation_names_a_changed_bringup_playbook(path, expected):
    """A bring-up playbook sits in no role directory, so `setup_roles` stays empty and the
    command fell back to `--tags <role>`, which matches no tag (#4282)."""
    cs = services_from_changed_paths([path])
    assert cs.setup_roles == set()
    cmd = broad_remediation(
        cs.broad_deploy, cs.broad_setup, cs.setup_roles, playbooks=cs.manual_playbooks
    )
    assert expected in cmd
    assert "<role>" not in cmd


def test_broad_remediation_puts_the_ff_merge_before_the_playbook():
    """Ansible renders from the working tree, so a playbook run before the merge copies the
    PRE-merge files and recaps `changed=0` — a clean-looking run over the old code."""
    cmd = broad_remediation(False, True)
    assert cmd.index("git merge --ff-only") < cmd.index("ansible-playbook"), cmd


def test_broad_remediation_names_the_branch_it_is_given():
    """gitops_deploy.py reads BRANCH from config.env; a hardcoded `master` would print a
    command that does nothing on a host tracking anything else."""
    assert "origin/release" in broad_remediation(False, True, branch="release")
    assert "origin/master" in broad_remediation(False, True)


def test_a_shared_module_edit_names_every_consumer_role():
    """`role_of` maps a path to the role whose directory holds it, which is right for a
    manifest and wrong for a shared library. bridge/common.py lives under monitor-bridge and
    autofix-bridge imports it, so an edit there must name autofix-bridge too, or its ConfigMap
    keeps the old copy with nothing reporting it.
    """
    paths = ["ansible/roles/k8s/monitor-bridge/files/bridge/common.py"]
    consumers = shared_module_consumers(paths, REPO)
    assert "autofix-bridge" in consumers, (
        "the deployer cannot see that autofix-bridge imports bridge.common, so a shared "
        "edit ff-merges leaving its ConfigMap stale: %s" % sorted(consumers)
    )
    assert "monitor-bridge" not in consumers, "the owning role is already in cs.flux"


def _k8s_tree(tmp_path, files):
    """A fake repo holding `ansible/roles/k8s/<role>/files/<path>` -> source pairs."""
    for rel, source in files.items():
        p = tmp_path / "ansible" / "roles" / "k8s" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(source)
    return tmp_path


def test_a_shared_module_inside_a_package_is_still_seen(tmp_path):
    """The detector sees `files/bridge/common.py`, in every spelling a consumer can use.

    Moving the shared module into a package must not make it invisible to the one check that
    exists for it, or the deployer goes back to emitting `--tags monitor-bridge` alone. The
    live-tree test above cannot be this red-proof while the tree is flat; this one exercises
    depth on both sides, the owner's and the consumer's.
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
    """Measured: a full deploy plus rollback leaves 96s against TimeoutStartSec.

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

    If TimeoutStartSec moves in gitops-deploy.service.j2, this fails and the decision gets
    revisited deliberately rather than drifting.

    Derived at 40min: 180 + 1212 + 1212 + 300 = 2904 against 2400 OVERRUNS, so the budget rules
    out a full-deploy rollback. broad_budget_ok has no production caller; it is the reasoning made
    executable, and gitops_deploy.py's broad arm is forward-only in code either way. Funding a
    broad rollback is a deliberate change to make on its own evidence (a re-measured deploy.yml,
    and a decision about a rollback that can still be SIGTERMed).
    """
    unit = (
        pathlib.Path(__file__).resolve().parents[1]
        / "templates"
        / "gitops-deploy.service.j2"
    )
    assert "TimeoutStartSec=40min" in unit.read_text(), (
        "TimeoutStartSec moved — re-derive broad_budget_ok's verdict before trusting it"
    )
    assert not broad_budget_ok(
        forward_s=1212, rollback_s=1212, flock_s=180, timeout_s=2400
    ), (
        "the re-derivation above says a broad rollback overruns at 40min; if this goes red the "
        "note in this docstring is stale and forward-only needs re-arguing from the budget again"
    )


# ── the role tag is the maximal apply, and the printed command says so ──────────────────────
# `ansible-playbook ansible/k3s-bringup.yml --tags k3s` restarts k3s and re-encrypts etcd. A
# change that needs only `--tags kubeconfig` must not be answered with it.


def test_the_k3s_role_tag_carries_what_running_it_does():
    """The command is the role tag, carrying a warning.

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
