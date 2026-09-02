"""Which plane a pushed path lands on: a scoped service deploy, a broad apply, or nothing.

A setup-role change must not name deploy.yml, an archived path must name nothing, and a
test-suite path must reach no host (test_deploy_changes_test_paths.py). The broad set
splits three ways -- what the deployer
applies itself, what it defers, and the bring-up playbooks it must never touch -- and every
case below says which side it lands on. The deploy.yml import guard is here because an
imported task file that classifies as EMPTY is the same silent ff-merge from the other side.
"""

# ansible/roles/setup/gitops_deploy/tests/test_deploy_changes_planes.py

import pathlib

import pytest
import yaml

from deploy_changes import ChangeSet, services_from_changed_paths, setup_tags_for


def test_shared_template_is_broad():
    paths = ["ansible/templates/resources.yml.j2"]
    cs = services_from_changed_paths(paths)
    assert cs.broad is True


def test_host_vars_is_broad():
    paths = ["ansible/inventory/host_vars/daniel-server.yml"]
    cs = services_from_changed_paths(paths)
    assert cs.broad is True


def test_requirements_yml_is_broad():
    # Galaxy collection bumps (Renovate) are installed by sops_setup, not deploy.yml — they
    # map to no service, so they must be flagged broad (defer-and-alert) rather than silently
    # ff-merged and left unapplied on the host.
    cs = services_from_changed_paths(["ansible/requirements.yml"])
    assert cs.broad is True
    assert cs.services == set()


# Setup roles are wired into initial_setup.yml, not deploy.yml — a change maps to no container
# service. Without the _BROAD_PREFIXES entry it would fall into the silent "docs-only" ff-merge
# and sit unapplied (worst case: a fix to gitops_deploy.py itself never takes effect). Must be
# flagged broad (defer-and-alert). Covers the deployer's own code, the notifier, and the
# by-hand bring-up playbooks.
def test_setup_role_change_is_broad():
    cs = services_from_changed_paths(
        ["ansible/roles/setup/gitops_deploy/files/gitops_deploy.py"]
    )
    assert cs.broad is True
    assert cs.services == set()


def test_renovate_notify_role_change_is_broad():
    cs = services_from_changed_paths(
        ["ansible/roles/setup/renovate_notify/templates/renovate-notify.service.j2"]
    )
    assert cs.broad is True
    assert cs.services == set()


def test_bringup_playbooks_are_broad():
    for p in (
        "ansible/initial_setup.yml",
        "ansible/bootstrap.yml",
        "ansible/k3s-bringup.yml",
    ):
        cs = services_from_changed_paths([p])
        assert cs.broad is True, p
        assert cs.services == set()


def test_ansible_cfg_is_broad_but_lockfiles_are_not():
    # ansible.cfg is a repo-root file read by every ansible-playbook the deployer runs (CWD = repo
    # root) but maps to no service — flag broad (defer-and-alert) so a bad value can't silently
    # ff-merge and mis-attribute a later deploy's failure.
    cs = services_from_changed_paths(["ansible.cfg"])
    assert cs.broad is True
    assert cs.services == set()
    # pyproject.toml / uv.lock churn weekly (lockFileMaintenance) and the broad path never ff-merges,
    # so flagging them broad parked the host and blocked every downstream image bump behind the stuck
    # lockfile (2026-07-15 review H1). They map to no service and aren't secrets, so they take the
    # silent ff-merge path; CI `uv lock --check` + the deploy health-gate back them up.
    for p in ("pyproject.toml", "uv.lock"):
        cs = services_from_changed_paths([p])
        assert cs.broad is False, p
        assert cs.services == set(), p
        assert cs.secrets is False, p
        assert cs.tasks == set(), p
        assert cs.meta == set(), p


# review-M1 (2026-07-16): a broad change is sub-classified by which manual playbook applies it, so
# the defer-alert names the RIGHT one. deploy.yml runs only container roles; the setup plane
# (roles/setup/, requirements.yml, bring-up playbooks) is applied by initial_setup.yml. Sending a
# setup-plane change to deploy.yml is a silent no-op that leaves it unapplied while a plain ff-merge
# clears the divergence — worst case a fix to gitops_deploy.py itself.
def test_broad_deploy_plane_flags_broad_deploy_not_setup():
    for p in (
        "ansible/templates/resources.yml.j2",
        "ansible/inventory/host_vars/daniel-server.yml",
        "ansible/roles/containers/common/templates/healthcheck.yml.j2",
        "ansible/deploy.yml",
        "ansible/filter_plugins/toposort.py",
        "ansible.cfg",
    ):
        cs = services_from_changed_paths([p])
        assert cs.broad is True, p
        assert cs.broad_deploy is True, p
        assert cs.broad_setup is False, p


def test_broad_setup_plane_flags_broad_setup_not_deploy():
    for p in (
        "ansible/requirements.yml",
        "ansible/roles/setup/gitops_deploy/files/gitops_deploy.py",
        "ansible/roles/setup/renovate_notify/templates/renovate-notify.service.j2",
        "ansible/initial_setup.yml",
        "ansible/bootstrap.yml",
        "ansible/k3s-bringup.yml",
    ):
        cs = services_from_changed_paths([p])
        assert cs.broad is True, p
        assert cs.broad_setup is True, p
        assert cs.broad_deploy is False, p


def test_broad_both_planes_flags_both():
    # A push touching both planes must flag both so the alert names both playbooks.
    cs = services_from_changed_paths(
        [
            "ansible/deploy.yml",
            "ansible/roles/setup/gitops_deploy/files/gitops_deploy.py",
        ]
    )
    assert cs.broad_deploy is True
    assert cs.broad_setup is True


def test_setup_tags_for_refuses_a_role_initial_setup_cannot_apply():
    """THE SILENT FAILURE. A tag matching nothing exits 0, so the deployer records a success.

    This is the automatic arm, not the alert text: returning `k3s` here made the tick run
    `initial_setup.yml --tags k3s`, change nothing, and report the change applied.
    """
    assert setup_tags_for(["ansible/roles/setup/k3s/tasks/node.yml"]) == set()
    assert (
        setup_tags_for(["ansible/roles/setup/common/templates/resolv.conf.j2"]) == set()
    )


def test_setup_tags_for_maps_a_renamed_tag():
    assert setup_tags_for(["ansible/roles/setup/chezmoi_setup/tasks/main.yml"]) == {
        "chezmoi"
    }


def test_setup_roles_are_recorded_for_both_broad_setup_arms():
    """k3s/ routes to k3s-bringup.yml (never applied here) and gitops_deploy/ applies itself;
    both remediations still need the role name."""
    cs = services_from_changed_paths(
        [
            "ansible/roles/setup/gitops_deploy/files/deploy_logic.py",
            "ansible/roles/setup/k3s/defaults/main.yml",
        ]
    )
    assert cs.setup_roles == {"gitops_deploy", "k3s"}


# --- broad three-way split -------------------------------------------------------------
#
# `broad` used to mean one thing: defer and alert. It now splits into what the deployer
# applies itself and what it must never touch, so every case below states which side it
# lands on. The manual set is the bring-up playbooks alone.

BROAD_AUTO_SETUP = ["ansible/roles/setup/renovate_notify/tasks/main.yml"]
BROAD_AUTO_SELF = ["ansible/roles/setup/gitops_deploy/files/gitops_deploy.py"]
BROAD_MANUAL_BRINGUP = ["ansible/k3s-bringup.yml"]
BROAD_AUTO_DEPLOY = ["ansible/templates/traefik.yml.j2"]


def test_ordinary_setup_role_is_clean_for_auto_apply():
    cs = services_from_changed_paths(BROAD_AUTO_SETUP)
    assert cs.broad and cs.broad_setup
    assert not cs.broad_manual
    assert setup_tags_for(BROAD_AUTO_SETUP) == {"renovate_notify"}


def test_the_deployers_own_role_applies_itself():
    """roles/setup/gitops_deploy/ sat in the manual set until 2026-09-01 on the claim that its
    handler restarts the unit running the tick. It does not: the handler is `state: started`,
    which Ansible's systemd module skips for an `activating` unit. Parking it instead stopped
    every other session's landing three times that day (#707, #712, #714). The DECIDED marker
    above `_BROAD_MANUAL_PREFIXES` carries the evidence."""
    cs = services_from_changed_paths(BROAD_AUTO_SELF)
    assert cs.broad and cs.broad_setup
    assert not cs.broad_manual
    assert setup_tags_for(BROAD_AUTO_SELF) == {"gitops_deploy"}


def test_bringup_playbooks_are_flagged_manual():
    cs = services_from_changed_paths(BROAD_MANUAL_BRINGUP)
    assert cs.broad and cs.broad_manual


def test_shared_template_is_clean_for_auto_apply():
    cs = services_from_changed_paths(BROAD_AUTO_DEPLOY)
    assert cs.broad and cs.broad_deploy
    assert not cs.broad_manual


def test_a_manual_path_bundled_with_an_auto_one_stays_manual():
    """Mixed pushes must not half-apply: the manual arm wins over the whole tick."""
    cs = services_from_changed_paths(BROAD_AUTO_SETUP + BROAD_MANUAL_BRINGUP)
    assert cs.broad_manual


def test_setup_tag_derivation_rejects_a_playbook_path():
    """An empty set means "cannot be applied automatically". Returning a bogus tag would
    be worse than nothing: `--tags` matching nothing makes Ansible exit 0, so the deployer
    would report a successful apply having done nothing at all."""
    assert setup_tags_for(["ansible/bootstrap.yml"]) == set()


def test_requirements_yml_derives_the_collections_tag():
    assert setup_tags_for(["ansible/requirements.yml"]) == {"collections"}


def test_setup_tag_derivation_skips_the_manual_set():
    assert setup_tags_for(BROAD_MANUAL_BRINGUP) == set()


def test_broad_stays_true_for_every_split_arm():
    """`broad` keeps its old meaning so every existing consumer is unchanged."""
    for paths in (
        BROAD_AUTO_SETUP,
        BROAD_AUTO_SELF,
        BROAD_MANUAL_BRINGUP,
        BROAD_AUTO_DEPLOY,
    ):
        assert services_from_changed_paths(paths).broad is True


# ── deploy.yml's imported task files must reach the classifier ────────────────────────────────

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[5]


def _task_imports(playbook) -> set[str]:
    """Every repo-relative task file a parsed playbook imports or includes.

    Fails closed on an argument shape it does not understand, the same bias
    `declared_denylist` takes: silently skipping one would let a converted call site drop out of
    the set while `assert imports` still passed on the remaining entries — the vacuous pass this
    guard exists to prevent, moved one level up into the parser.
    """
    found: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(key, str) and key.split(".")[-1] in (
                    "import_tasks",
                    "include_tasks",
                ):
                    found.add("ansible/" + _import_path(key, value))
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(playbook)
    return found


def _import_path(key: str, value) -> str:
    """The task file one import_tasks/include_tasks argument names, in either accepted form."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        # `file:` is the documented key; `_raw_params` is what the free-form string becomes when
        # it is written alongside other options.
        for option in ("file", "_raw_params"):
            if isinstance(value.get(option), str):
                return value[option]
    raise AssertionError(
        "cannot read the task file out of `%s: %r` — teach _import_path this shape rather than "
        "letting the import drop silently out of the guard" % (key, value)
    )


def _deploy_yml_task_imports() -> set[str]:
    return _task_imports(
        yaml.safe_load((_REPO_ROOT / "ansible" / "deploy.yml").read_text())
    )


def test_task_import_parsing_reads_the_dict_form_too():
    """The dict form is equally valid Ansible, and deploy.yml uses only the string form today.

    Without this, converting a call site to `include_tasks: {file: ...}` would drop it from the
    guard while the guard stayed green on the remaining imports.
    """
    string_form = yaml.safe_load(
        "- ansible.builtin.include_tasks: tasks/k8s_batch.yml\n"
    )
    dict_form = yaml.safe_load(
        "- ansible.builtin.include_tasks:\n    file: tasks/k8s_batch.yml\n"
    )
    assert _task_imports(string_form) == {"ansible/tasks/k8s_batch.yml"}
    assert _task_imports(dict_form) == {"ansible/tasks/k8s_batch.yml"}

    with pytest.raises(AssertionError, match="cannot read the task file"):
        _task_imports(
            yaml.safe_load("- ansible.builtin.include_tasks:\n    apply: {}\n")
        )


def test_every_task_file_deploy_yml_imports_is_visible_to_the_classifier():
    """A file deploy.yml imports must not classify as an EMPTY ChangeSet.

    `ansible/deploy.yml` was broad, but its sibling task dirs matched nothing: every _ACTIVE_*
    regex is anchored to `ansible/roles/`. main() has no catch-all — `if not cs.services:`
    ff-merges unconditionally and the alert helpers no-op on empty fields — so an
    empty-because-unclassified ChangeSet was indistinguishable from an empty-because-docs one:
    silent ff-merge, no alert, no deploy, on files that change what EVERY deploy does.

    Derived from the playbook rather than pinned to today's three paths, so a newly-imported task
    file cannot fall through the classifier the same way.
    """
    imports = _deploy_yml_task_imports()
    assert imports, "parsed no import_tasks/include_tasks out of ansible/deploy.yml"

    invisible = sorted(
        p for p in imports if services_from_changed_paths([p]) == ChangeSet()
    )
    assert not invisible, (
        "deploy.yml imports these task files but the classifier returns an empty ChangeSet for "
        "them, so a push touching one silently ff-merges with no alert and no deploy: %s"
        % invisible
    )
