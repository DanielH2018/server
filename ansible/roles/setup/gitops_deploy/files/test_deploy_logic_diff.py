"""Reading a git diff into "what must be deployed".

Getting this wrong is silent in both directions. Too narrow and a changed template never
reaches the host while the tick reports success; too broad and every push redeploys the estate.
The plane split matters for the same reason — a setup-role change must not name deploy.yml, and
an archived path must name nothing at all.
"""

# ansible/roles/setup/gitops_deploy/files/test_deploy_logic.py

from deploy_logic import (
    services_from_changed_paths,
    broad_remediation,
    deferred_service_alerts,
    setup_tags_for,
)


def test_single_service_template():
    paths = ["ansible/roles/containers/cadvisor/templates/docker-compose.yml.j2"]
    cs = services_from_changed_paths(paths)
    assert cs.services == {"cadvisor"}
    assert cs.broad is False


def test_multiple_services():
    paths = [
        "ansible/roles/containers/cadvisor/templates/docker-compose.yml.j2",
        "ansible/roles/containers/couchdb/templates/docker-compose.yml.j2",
    ]
    cs = services_from_changed_paths(paths)
    assert cs.services == {"cadvisor", "couchdb"}
    assert cs.broad is False


def test_archived_service_is_ignored():
    paths = [
        "ansible/roles/containers/archive/duplicati/templates/docker-compose.yml.j2"
    ]
    cs = services_from_changed_paths(paths)
    assert cs.services == set()
    assert cs.broad is False


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


def test_unrelated_path_ignored():
    paths = ["docs/superpowers/specs/x.md", "README.md"]
    cs = services_from_changed_paths(paths)
    assert cs.services == set()
    assert cs.broad is False


# A secrets-only push (e.g. a manual rotation of an assisted/external secret from another
# machine) maps to no service and isn't broad, so it used to fall into the silent
# `git merge --ff-only; return` path — the rotated value then sat stale in the running
# container with no redeploy and no alert. It must instead be flagged so the deployer
# defers-and-alerts. (Adding ansible/vars/ to _BROAD_PREFIXES was rejected: that would also
# force the /add-secret flow — secrets.yml + the consuming template together — into a manual
# full deploy instead of the correct single-service deploy.)
def test_secrets_only_change_flags_secrets_not_broad():
    cs = services_from_changed_paths(["ansible/vars/secrets.yml"])
    assert cs.secrets is True
    assert cs.services == set()
    assert cs.broad is False


def test_secrets_with_service_template_still_deploys_that_service():
    # The /add-secret flow commits secrets.yml + the consuming template together — the
    # service maps, so it deploys normally (applying the secret); the flag is also set.
    cs = services_from_changed_paths(
        [
            "ansible/vars/secrets.yml",
            "ansible/roles/containers/karakeep/templates/docker-compose.yml.j2",
        ]
    )
    assert cs.services == {"karakeep"}
    assert cs.secrets is True
    assert cs.broad is False


def test_no_secrets_change_leaves_flag_false():
    cs = services_from_changed_paths(
        ["ansible/roles/containers/cadvisor/templates/docker-compose.yml.j2"]
    )
    assert cs.secrets is False


def test_secret_rotation_registry_only_is_not_secrets():
    # The plaintext registry (names/dates, no value change) needs no redeploy — a silent
    # ff is correct, so it must NOT trip the secrets flag.
    cs = services_from_changed_paths(["ansible/secret_rotation.yml"])
    assert cs.secrets is False
    assert cs.services == set()
    assert cs.broad is False


# M2: a service-scoped change to a bind-mounted CONFIG template or files/ asset (not just the
# compose) must map to that service for a scoped, health-gated redeploy — closing the GitOps loop
# so live config matches master. Previously these fell into the silent ff-merge "docs-only" path
# (the config sat stale in the running container with no redeploy and no alert).
def test_config_template_change_maps_to_service():
    cs = services_from_changed_paths(
        ["ansible/roles/containers/prometheus/templates/prometheus.yml.j2"]
    )
    assert cs.services == {"prometheus"}
    assert cs.broad is False


def test_files_asset_change_maps_to_service():
    # Must be a DOCKER role path: k8s paths defer-and-alert instead of mapping to a
    # service. ical-proxy was the fixture until its files/ moved into roles/k8s
    # (2026-08-14) along with the other config-source roles, leaving the Pi's services as
    # the only Docker ones. The asset name is illustrative -- this parses paths, it does
    # not stat them.
    cs = services_from_changed_paths(
        ["ansible/roles/containers/glances/files/glances.conf"]
    )
    assert cs.services == {"glances"}
    assert cs.broad is False


def test_archived_config_change_is_ignored():
    cs = services_from_changed_paths(
        ["ansible/roles/containers/archive/duplicati/templates/foo.yml.j2"]
    )
    assert cs.services == set()
    assert cs.broad is False


def test_common_role_change_stays_broad_not_scoped():
    # common/ is the shared deploy path — it must remain BROAD (manual full deploy), so the
    # broad-prefix check must win over the new service-scoped config match.
    cs = services_from_changed_paths(
        ["ansible/roles/containers/common/templates/healthcheck.yml.j2"]
    )
    assert cs.broad is True
    assert cs.services == set()


def test_role_tasks_change_flags_tasks_not_deploy():
    # tasks/ isn't auto-deployed (structural — manual), but it must be FLAGGED so the deployer
    # defers-and-alerts instead of silently ff-merging: a tasks/ change alters what a deploy does,
    # so left unapplied with no signal it's the exact silent-drift the secrets/requirements paths
    # already close. It maps to cs.tasks (for the alert), NOT cs.services (no scoped redeploy).
    cs = services_from_changed_paths(
        ["ansible/roles/containers/prometheus/tasks/main.yml"]
    )
    assert cs.tasks == {"prometheus"}
    assert cs.services == set()
    assert cs.broad is False
    assert cs.secrets is False


def test_role_docs_do_not_trigger_deploy_or_flag():
    # A CLAUDE.md / doc edit is genuinely no-op (manual, as before) — not even flagged.
    cs = services_from_changed_paths(["ansible/roles/containers/prometheus/CLAUDE.md"])
    assert cs.services == set()
    assert cs.tasks == set()
    assert cs.broad is False


def test_common_tasks_change_stays_broad_not_tasks():
    # common/ is the shared deploy path — a tasks change there is BROAD (manual full deploy); the
    # broad-prefix check must win over the new tasks match.
    cs = services_from_changed_paths(
        ["ansible/roles/containers/common/tasks/docker_deploy.yml"]
    )
    assert cs.broad is True
    assert cs.tasks == set()
    assert cs.services == set()


def test_archived_tasks_change_is_ignored():
    cs = services_from_changed_paths(
        ["ansible/roles/containers/archive/duplicati/tasks/main.yml"]
    )
    assert cs.tasks == set()
    assert cs.services == set()
    assert cs.broad is False


def test_template_and_tasks_same_service_deploys_and_flags_tasks():
    # A push that changes both a template and tasks/ for the same service deploys it (the scoped
    # --tags redeploy reruns the whole role incl. tasks), and also records the tasks flag.
    cs = services_from_changed_paths(
        [
            "ansible/roles/containers/prometheus/templates/prometheus.yml.j2",
            "ansible/roles/containers/prometheus/tasks/main.yml",
        ]
    )
    assert cs.services == {"prometheus"}
    assert cs.tasks == {"prometheus"}


# M4: meta/deps.yml drives the cross-service toposort (deploy ORDER + dep CLOSURE via
# filter_plugins/toposort.py). It isn't auto-deployed (structural, like tasks/), but a meta-only
# push must be FLAGGED (defer-and-alert), not silently ff-merged as a docs edit — otherwise the
# graph change is invisible. Maps to cs.meta (for the alert), NOT cs.services.
def test_role_meta_change_flags_meta_not_deploy():
    cs = services_from_changed_paths(["ansible/roles/containers/dozzle/meta/deps.yml"])
    assert cs.meta == {"dozzle"}
    assert cs.services == set()
    assert cs.tasks == set()
    assert cs.broad is False
    assert cs.secrets is False


# k8s roles (ansible/roles/k8s/<role>/...) matched NONE of the regexes above (all containers/-
# scoped), so services_from_changed_paths returned an EMPTY ChangeSet and main()'s `if not
# cs.services:` branch took it as a plain docs-only ff-merge — silent, on every has_gitops host
# (daniel-box, all 41 services platform: k8s). Maps to cs.k8s (defer-and-alert), never cs.services
# — this deployer has no mechanism that ever auto-deploys a k8s-platform role.
def test_k8s_role_change_flags_k8s_not_services():
    cs = services_from_changed_paths(
        ["ansible/roles/k8s/authelia/templates/deployment.yaml.j2"]
    )
    assert cs.k8s == {"authelia"}
    assert cs.services == set()
    assert cs.broad is False
    assert cs.tasks == set()


def test_k8s_role_matches_any_subdir_not_just_templates():
    # Unlike containers/ (split into templates/tasks/meta channels), a k8s role has no separate
    # auto-deploy path for any of its subdirs to scope against — the whole role dir shares one
    # channel.
    cs = services_from_changed_paths(["ansible/roles/k8s/authelia/tasks/main.yml"])
    assert cs.k8s == {"authelia"}
    assert cs.services == set()


def test_k8s_role_docs_stay_silent():
    cs = services_from_changed_paths(["ansible/roles/k8s/authelia/CLAUDE.md"])
    assert cs.k8s == set()
    assert cs.services == set()
    assert cs.broad is False


def test_k8s_and_docker_service_changes_are_independent():
    cs = services_from_changed_paths(
        [
            "ansible/roles/k8s/authelia/templates/deployment.yaml.j2",
            "ansible/roles/containers/cadvisor/templates/docker-compose.yml.j2",
        ]
    )
    assert cs.k8s == {"authelia"}
    assert cs.services == {"cadvisor"}


# L2: a change under a container role's defaults/, vars/, or handlers/ matched none of the
# config/tasks/meta regexes and fell through to the silent docs-only ff-merge — a structural change
# that alters what a deploy does, applied with no signal. The _ACTIVE_ROLE catch-all flags it on the
# tasks channel (defer-and-alert), so a future role adding one of these dirs can't regress silently.
def test_role_defaults_change_flags_tasks_not_deploy():
    cs = services_from_changed_paths(
        ["ansible/roles/containers/prometheus/defaults/main.yml"]
    )
    assert cs.tasks == {"prometheus"}
    assert cs.services == set()
    assert cs.broad is False
    assert cs.secrets is False


def test_role_vars_and_handlers_change_flag_tasks():
    for sub in ("vars", "handlers"):
        cs = services_from_changed_paths(
            [f"ansible/roles/containers/sonarr/{sub}/main.yml"]
        )
        assert cs.tasks == {"sonarr"}, sub
        assert cs.services == set(), sub


def test_role_root_non_md_file_flags_tasks():
    # A non-doc file at the role root (not templates/files/tasks/meta) is structural too.
    cs = services_from_changed_paths(["ansible/roles/containers/prometheus/vars.yml"])
    assert cs.tasks == {"prometheus"}


def test_role_readme_md_stays_silent_like_claude_md():
    # *.md is a doc — the catch-all must not flag it (regression guard for the .md exclusion).
    cs = services_from_changed_paths(["ansible/roles/containers/prometheus/README.md"])
    assert cs.tasks == set()
    assert cs.services == set()
    assert cs.broad is False


def test_archived_defaults_change_is_ignored():
    cs = services_from_changed_paths(
        ["ansible/roles/containers/archive/duplicati/defaults/main.yml"]
    )
    assert cs.tasks == set()
    assert cs.services == set()
    assert cs.broad is False


def test_common_defaults_change_stays_broad_not_tasks():
    # common/ is the shared deploy path — a defaults change there is BROAD (manual full deploy);
    # the broad-prefix check must win over the catch-all.
    cs = services_from_changed_paths(
        ["ansible/roles/containers/common/defaults/main.yml"]
    )
    assert cs.broad is True
    assert cs.tasks == set()
    assert cs.services == set()


def test_common_meta_change_stays_broad_not_meta():
    # common/ is the shared deploy path — a meta change there is BROAD (manual full deploy); the
    # broad-prefix check must win over the new meta match.
    cs = services_from_changed_paths(["ansible/roles/containers/common/meta/main.yml"])
    assert cs.broad is True
    assert cs.meta == set()
    assert cs.services == set()


def test_archived_meta_change_is_ignored():
    cs = services_from_changed_paths(
        ["ansible/roles/containers/archive/duplicati/meta/deps.yml"]
    )
    assert cs.meta == set()
    assert cs.services == set()
    assert cs.broad is False


def test_template_and_meta_same_service_deploys_and_flags_meta():
    # A push changing both a template and meta/ for the same service deploys it (scoped --tags)
    # and records the meta flag too — the combined-push case that used to swallow the meta change.
    cs = services_from_changed_paths(
        [
            "ansible/roles/containers/dozzle/templates/docker-compose.yml.j2",
            "ansible/roles/containers/dozzle/meta/deps.yml",
        ]
    )
    assert cs.services == {"dozzle"}
    assert cs.meta == {"dozzle"}


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


def test_config_change_with_compose_change_dedupes_to_one_service():
    cs = services_from_changed_paths(
        [
            "ansible/roles/containers/traefik/templates/config.yml.j2",
            "ansible/roles/containers/traefik/templates/docker-compose.yml.j2",
        ]
    )
    assert cs.services == {"traefik"}


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


# ── test-suite paths reach no host, so they drive no deploy decision ────────────────────
#
# Every prefix in this module matches on path alone, which made a test file read as whatever
# plane it happened to sit under. PR #707 changed three of them and hit two arms at once: a
# test under roles/setup/gitops_deploy/ set broad_manual and parked the tick for every session,
# and one under roles/k8s/<svc>/files/ set cs.k8s and defer-alerted. Clearing it took an
# operator running the playbook by hand and then an ff-merge (2026-09-01).
#
# The pairs below are clean/flagged per rule: a guard that classifies everything as a test and
# one that classifies nothing are indistinguishable from the passing side alone.

TEST_ONLY_PATHS = [
    # The repo-wide guards, plus the two support modules there that match no name pattern.
    "ansible/tests/test_k8s_manifests.py",
    "ansible/tests/_helpers.py",
    "ansible/tests/conftest.py",
    # A test beside the module it covers — the layout every roles/*/*/files/ suite uses.
    "ansible/roles/setup/gitops_deploy/files/test_deploy_logic_health.py",
    "ansible/roles/k8s/qbittorrent/files/test_apply_prefs.py",
    "ansible/roles/k8s/monitor-bridge/files/conftest.py",
    # A role-local tests/ directory.
    "ansible/roles/k8s/home-assistant/tests/test_fan_macros.py",
]


def test_a_test_only_push_is_clean():
    """It must reach gitops_deploy.py's `if not cs.services` branch, which ff-merges."""
    cs = services_from_changed_paths(TEST_ONLY_PATHS)
    assert cs.broad is False
    assert cs.broad_manual is False
    assert cs.services == set()
    assert cs.k8s == set()
    assert cs.tasks == set()
    assert cs.secrets is False


def test_the_deployers_own_module_is_still_flagged():
    """The rejecting half. deploy_logic.py sits in the same directory as the test above and
    must still read as a setup-plane change the tick applies — not as the empty ChangeSet a
    test-only push produces, which would ff-merge it and leave the host on the old code."""
    cs = services_from_changed_paths(
        ["ansible/roles/setup/gitops_deploy/files/deploy_logic.py"]
    )
    assert cs.broad is True and cs.broad_setup is True
    assert cs.setup_roles == {"gitops_deploy"}


def test_a_test_file_does_not_disarm_a_real_change_beside_it():
    """The case an over-broad predicate breaks. The plane flags are ORed across the push, so
    one exempt path must not clear the flag a sibling set — a half-applied broad change is
    exactly what the arm exists to prevent."""
    cs = services_from_changed_paths(
        [
            "ansible/roles/setup/gitops_deploy/files/test_deploy_logic_health.py",
            "ansible/roles/setup/gitops_deploy/files/deploy_logic.py",
        ]
    )
    assert cs.broad_setup is True
    assert cs.setup_roles == {"gitops_deploy"}


def test_a_k8s_service_change_is_still_flagged_beside_its_test():
    """The same rejecting half one plane over: the test is exempt, the manifest is not."""
    cs = services_from_changed_paths(
        [
            "ansible/roles/k8s/qbittorrent/files/test_apply_prefs.py",
            "ansible/roles/k8s/qbittorrent/templates/deployment.yaml.j2",
        ]
    )
    assert "qbittorrent" in cs.k8s


def test_a_path_merely_containing_test_is_not_exempt():
    """`latest`/`contest` end in `test`; a directory named `tests` is not a path segment
    called `testdata`. Substring matching here would exempt real templates."""
    for path in (
        "ansible/roles/k8s/sonarr/templates/latest.yaml.j2",
        "ansible/roles/k8s/sonarr/testsuite-config/values.yml.j2",
        "ansible/roles/setup/gitops_deploy/files/pytest_helper.py",
    ):
        cs = services_from_changed_paths([path])
        assert cs.broad or cs.services or cs.k8s or cs.tasks or cs.meta, path
