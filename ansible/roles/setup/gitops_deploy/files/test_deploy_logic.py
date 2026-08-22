# ansible/roles/setup/gitops_deploy/files/test_deploy_logic.py
import ast
import os
import pathlib
import subprocess
import sys
import time
from datetime import datetime

import pytest
import yaml

from deploy_logic import (
    ChangeSet,
    services_from_changed_paths,
    broad_remediation,
    deferred_service_alerts,
    next_action,
    is_diverged,
    behind_marker,
    container_names,
    containers_to_gate,
    should_alert_dirty,
    dirty_alert_slot,
    health_decision,
    health_settles,
    gate_services,
    apply_send_result,
    apply_drain_result,
    declared_k8s_services,
    declared_services,
    reroute_k8s_services,
    stale_rendered_services,
    is_image_only_diff,
    split_k8s_auto_deploy,
    ci_verdict,
    declared_denylist,
    declares_snapshot_claims,
    rollback_volume_revert_note,
    SHARED_K8S_ROLES,
    k8s_role_paths,
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


def test_next_action_noop_when_in_sync():
    assert next_action("aaa", "aaa", None) == "noop"


def test_next_action_skip_when_origin_is_hold():
    assert next_action("aaa", "bad", "bad") == "skip_hold"


def test_next_action_deploy_when_origin_ahead():
    assert next_action("aaa", "bbb", None) == "deploy"


def test_next_action_deploy_when_hold_is_stale():
    # origin advanced past the held bad SHA (operator reverted) -> deploy again
    assert next_action("aaa", "ccc", "bad") == "deploy"


def test_next_action_dirty_tree_skips_even_in_sync():
    # A dirty working tree is a *healthy* skip (operator mid-edit), not an outage.
    # It must short-circuit to "dirty" so main() can still push liveness instead
    # of going silent and falsely tripping the push monitor's dead-man's-switch.
    assert next_action("aaa", "aaa", None, dirty=True) == "dirty"


def test_next_action_dirty_tree_never_deploys():
    # Must NOT deploy from a dirty tree even when origin has advanced — dirty
    # takes precedence over every other outcome.
    assert next_action("aaa", "bbb", None, dirty=True) == "dirty"


def test_next_action_clean_tree_still_deploys():
    # Regression: a clean tree (the default) behaves exactly as before.
    assert next_action("aaa", "bbb", None, dirty=False) == "deploy"


# The deployer is pull-based and only ever fast-forwards: it must act ONLY when
# origin is strictly ahead of local. When the operator has committed locally but
# not pushed, origin is an *ancestor* of local (origin_ahead=False). The old code
# saw origin != local and returned "deploy", then diffed local..origin (the reverse
# of the un-pushed commits) and mis-fired a deploy + false rollback. Must be a no-op.
def test_next_action_noop_when_local_ahead_of_origin():
    assert next_action("localnew", "originold", None, origin_ahead=False) == "noop"


def test_next_action_deploy_requires_origin_ahead():
    # The normal pull path: origin strictly ahead (the default) still deploys.
    assert next_action("aaa", "bbb", None, origin_ahead=True) == "deploy"


def test_next_action_dirty_precedes_origin_ahead_check():
    # dirty still short-circuits even when origin isn't ahead.
    assert (
        next_action("localnew", "originold", None, dirty=True, origin_ahead=False)
        == "dirty"
    )


# is_diverged: local↔origin diverged (neither an ancestor of the other) → the deployer noops
# forever while origin's new commits never deploy; surfaced via GitOps Status (review L3).
def test_is_diverged_true_when_neither_is_ancestor():
    assert is_diverged("originX", "localY", origin_ahead=False, local_ahead=False)


def test_is_diverged_false_when_origin_ahead():
    # normal pull path — fast-forwardable, deploys.
    assert not is_diverged("originX", "localY", origin_ahead=True, local_ahead=False)


def test_is_diverged_false_when_local_ahead_unpushed():
    # committed-but-unpushed local commit is a plain noop (secret-rotate's domain), not divergence.
    assert not is_diverged("originX", "localY", origin_ahead=False, local_ahead=True)


def test_is_diverged_false_when_in_sync():
    assert not is_diverged("same", "same", origin_ahead=True, local_ahead=True)


# The health gate must only check services actually deployed on THIS host. A
# changed template for an other-host-only service (dozzle is daniel-pi-only)
# renders no compose here, so containers_for() reads no file and passes None.
# Gating it would poll a phantom container until timeout and false-rollback.
def test_containers_to_gate_skips_service_not_on_this_host():
    assert containers_to_gate(None, "dozzle") == []


def test_containers_to_gate_uses_rendered_container_names():
    compose = "    container_name: scrutiny-influxdb\n    container_name: scrutiny\n"
    assert containers_to_gate(compose, "scrutiny") == ["scrutiny-influxdb", "scrutiny"]


def test_containers_to_gate_falls_back_to_service_when_compose_names_none():
    # Present compose that declares no container_name -> gate the role/service name.
    assert containers_to_gate("    image: foo\n", "freshrss") == ["freshrss"]


# A role may run several containers; the bumped image's container is often NOT
# the role-named one (e.g. cadvisor lives in the prometheus role). The health
# gate must inspect the actual container_name values from the rendered compose.
def test_container_names_multi_container():
    compose = (
        "services:\n"
        "  influxdb:\n"
        "    container_name: scrutiny-influxdb\n"
        "  web:\n"
        "    container_name: scrutiny\n"
        "  collector:\n"
        "    container_name: scrutiny-collector\n"
    )
    assert container_names(compose) == [
        "scrutiny-influxdb",
        "scrutiny",
        "scrutiny-collector",
    ]


def test_container_names_strips_quotes():
    assert container_names('    container_name: "cadvisor"\n') == ["cadvisor"]


def test_container_names_ignores_other_keys():
    compose = (
        "    image: ghcr.io/google/cadvisor:v0.53.0\n    restart: unless-stopped\n"
    )
    assert container_names(compose) == []


def test_container_names_dedupes():
    assert container_names("    container_name: a\n    container_name: a\n") == ["a"]


def test_container_names_empty():
    assert container_names("") == []


# The dirty-tree alert fires on every 30-min tick by default, which spams the
# webhook through a long edit session. should_alert_dirty() throttles it to at
# most once per slot — a morning slot (08:00-19:59 CT) and an evening slot
# (>=20:00 CT) — and never before the morning hour, so an overnight-dirty tree
# pages once at ~8 AM and once at ~8 PM, not all night.
def test_dirty_alert_fires_first_tick_after_8am_when_never_alerted():
    # Overnight-dirty tree, first eligible morning tick, no prior alert today.
    now = datetime(2026, 6, 20, 8, 0)
    assert should_alert_dirty(now, None) is True


def test_dirty_alert_suppressed_before_8am():
    # A pre-dawn tick must stay silent even if we've never alerted.
    now = datetime(2026, 6, 20, 7, 59)
    assert should_alert_dirty(now, None) is False


def test_dirty_alert_suppressed_when_already_alerted_this_morning():
    # Second (and every later) morning tick after the morning alert.
    now = datetime(2026, 6, 20, 12, 30)
    assert should_alert_dirty(now, "2026-06-20:am") is False


def test_dirty_alert_fires_in_evening_after_morning_alert():
    # Still dirty at night after the morning page -> the evening slot fires once.
    now = datetime(2026, 6, 20, 20, 0)
    assert should_alert_dirty(now, "2026-06-20:am") is True


def test_dirty_alert_suppressed_when_already_alerted_this_evening():
    # A later evening tick after the evening alert -> no repeat.
    now = datetime(2026, 6, 20, 22, 15)
    assert should_alert_dirty(now, "2026-06-20:pm") is False


def test_dirty_alert_fires_again_next_morning():
    # Still dirty the next morning after last night's page -> a fresh reminder.
    now = datetime(2026, 6, 21, 8, 15)
    assert should_alert_dirty(now, "2026-06-20:pm") is True


def test_dirty_alert_at_exactly_8am_boundary_inclusive():
    now = datetime(2026, 6, 20, 8, 0)
    assert should_alert_dirty(now, "2026-06-19:pm") is True


def test_dirty_alert_at_exactly_8pm_boundary_inclusive():
    now = datetime(2026, 6, 20, 20, 0)
    assert should_alert_dirty(now, "2026-06-20:am") is True


def test_dirty_alert_predawn_tick_after_evening_alert_stays_quiet():
    # Dirty from 8 PM through 2 AM: the after-midnight tick is before the morning
    # slot, so it must not re-page even though the date has rolled over.
    now = datetime(2026, 6, 21, 2, 0)
    assert should_alert_dirty(now, "2026-06-20:pm") is False


def test_dirty_alert_newly_dirtied_after_8am_alerts_once():
    # Tree goes dirty mid-afternoon with no alert recorded today -> one alert now.
    now = datetime(2026, 6, 20, 15, 0)
    assert should_alert_dirty(now, None) is True


def test_dirty_alert_custom_hours():
    # Custom morning/evening hours push both slot boundaries.
    assert should_alert_dirty(datetime(2026, 6, 20, 8, 0), None, 9, 21) is False
    assert should_alert_dirty(datetime(2026, 6, 20, 9, 0), None, 9, 21) is True
    assert (
        should_alert_dirty(datetime(2026, 6, 20, 20, 0), "2026-06-20:am", 9, 21)
        is False
    )
    assert (
        should_alert_dirty(datetime(2026, 6, 20, 21, 0), "2026-06-20:am", 9, 21) is True
    )


def test_dirty_alert_slot_keys():
    assert dirty_alert_slot(datetime(2026, 6, 20, 7, 59)) is None
    assert dirty_alert_slot(datetime(2026, 6, 20, 8, 0)) == "2026-06-20:am"
    assert dirty_alert_slot(datetime(2026, 6, 20, 19, 59)) == "2026-06-20:am"
    assert dirty_alert_slot(datetime(2026, 6, 20, 20, 0)) == "2026-06-20:pm"
    assert dirty_alert_slot(datetime(2026, 6, 20, 23, 30)) == "2026-06-20:pm"


# The health gate is the deployer's rollback decision: health_ok() polls docker and,
# for an image with no HEALTHCHECK, requires `settle_checks` consecutive 'running'
# samples (the boot-then-crash guard) before passing. health_ok()'s I/O loop now
# delegates the per-sample pass/wait + streak transition to the pure health_decision();
# health_settles() folds it over a sample sequence (what the live poll loop would
# conclude). These were previously the one untested piece of safety-critical pipeline.
def test_health_decision_healthy_passes_immediately():
    # 'healthy' passes the gate on the first sample; streak left untouched.
    assert health_decision("healthy", False, 0) == ("healthy", 0)


def test_health_decision_unhealthy_waits_and_resets_streak():
    # 'unhealthy' is never a pass and clears any running streak built up so far.
    assert health_decision("unhealthy", False, 2) == ("wait", 0)


def test_health_decision_starting_waits_and_resets_streak():
    assert health_decision("starting", False, 2) == ("wait", 0)


def test_health_decision_no_healthcheck_builds_running_streak():
    # No HEALTHCHECK (status ''): each 'running' sample increments the streak; it
    # only passes once it reaches settle_checks consecutive samples.
    assert health_decision("", True, 0, settle_checks=3) == ("wait", 1)
    assert health_decision("", True, 1, settle_checks=3) == ("wait", 2)
    assert health_decision("", True, 2, settle_checks=3) == ("healthy", 3)


def test_health_decision_no_healthcheck_not_running_resets_streak():
    # A container that stops 'running' mid-settle resets the streak to 0.
    assert health_decision("", False, 2, settle_checks=3) == ("wait", 0)


def test_health_settles_healthy_first_sample():
    assert health_settles([("healthy", False)]) is True


def test_health_settles_no_healthcheck_sustained_running():
    # Three consecutive 'running' samples (no healthcheck) settle the gate.
    assert health_settles([("", True), ("", True), ("", True)], settle_checks=3) is True


def test_health_settles_no_healthcheck_two_running_not_enough():
    # Only two 'running' samples before polls run out -> never settles (would time out).
    assert health_settles([("", True), ("", True)], settle_checks=3) is False


def test_health_settles_boot_then_crash_loop_never_settles():
    # Boots 'running' twice, crashes (not running), repeats — the streak resets and
    # never reaches 3 consecutive, so the gate times out and rolls back. This is the
    # exact case a single 'running' sample would have wrongly passed.
    samples = [("", True), ("", True), ("", False), ("", True), ("", True), ("", False)]
    assert health_settles(samples, settle_checks=3) is False


def test_health_settles_unhealthy_then_recovers():
    # 'starting'/'unhealthy' while booting, then 'healthy' -> passes.
    samples = [("starting", False), ("unhealthy", False), ("healthy", False)]
    assert health_settles(samples) is True


def test_health_settles_never_healthy_times_out():
    # Perpetually 'unhealthy' -> the gate fails (rollback).
    assert health_settles([("unhealthy", False)] * 5) is False


# gate_services bounds the TOTAL wall-clock spent health-gating a deploy batch so the gate +
# rollback finishes inside the unit's TimeoutStartSec. Without the cap, a batch with several
# containers each polling to HEALTH_TIMEOUT_S could overrun the timeout; systemd would then
# SIGTERM the deployer before the rollback + hold ran, leaving the bad commit live. Clock + health
# probe are injected so the budget logic is testable with no docker / sleep / wall-clock.
def test_gate_services_all_healthy_returns_empty():
    # Every service healthy, budget never reached -> nothing to roll back.
    assert gate_services({"a", "b", "c"}, lambda s, dl: True, 100.0, lambda: 0.0) == []


def test_gate_services_reports_only_unhealthy():
    assert gate_services(
        {"a", "b", "c"}, lambda s, dl: s != "b", 100.0, lambda: 0.0
    ) == ["b"]


def test_gate_services_gates_in_sorted_deterministic_order():
    assert gate_services({"c", "a", "b"}, lambda s, dl: False, 100.0, lambda: 0.0) == [
        "a",
        "b",
        "c",
    ]


def test_gate_services_budget_exhausted_midway_fails_the_rest():
    # Clock: 0 before 'a' (gated, healthy), then 100 (>= deadline) before 'b' -> 'b' and 'c' are
    # marked failed without polling them, so the rollback fires while there's still time.
    ticks = iter([0.0, 100.0, 100.0])
    assert gate_services(
        {"a", "b", "c"}, lambda s, dl: True, 100.0, lambda: next(ticks)
    ) == ["b", "c"]


def test_gate_services_budget_exhausted_before_first_fails_all():
    # Deploy ate the whole budget: the clock is already past the deadline on the first check, so
    # every service is failed (health unverifiable -> roll back to be safe).
    assert gate_services({"a", "b"}, lambda s, dl: True, 100.0, lambda: 999.0) == [
        "a",
        "b",
    ]


def test_gate_services_threads_deadline_into_health_fn():
    # Each health check receives the gate deadline so one slow container's own poll can't overrun it.
    seen = []

    def health(s, dl):
        seen.append(dl)
        return True

    gate_services({"a"}, health, 55.0, lambda: 0.0)
    assert seen == [55.0]


# The pending-alert queue reconciliation (gitops_deploy.deliver / drain_pending) is pure keep/drop
# logic lifted here so it's exercised without the un-importable deployer's discord() I/O. deliver()
# clears a key on a confirmed send and (re)queues its content on a failure; drain() drops only the
# entries a redelivery confirmed. A regression here silently drops (or never clears) a post-merge alert.
def test_apply_send_result_clears_key_on_delivery():
    assert apply_send_result({"secrets:abc": "msg"}, "secrets:abc", "msg", True) == {}


def test_apply_send_result_keeps_other_keys_on_delivery():
    pending = {"secrets:abc": "m1", "tasks:def": "m2"}
    assert apply_send_result(pending, "secrets:abc", "m1", True) == {"tasks:def": "m2"}


def test_apply_send_result_queues_content_on_failure():
    assert apply_send_result({}, "secrets:abc", "msg", False) == {"secrets:abc": "msg"}


def test_apply_send_result_requeues_updated_content_on_failure():
    # A re-detected alert with fresh content overwrites the stale queued copy.
    assert apply_send_result({"broad:abc": "old"}, "broad:abc", "new", False) == {
        "broad:abc": "new"
    }


def test_apply_send_result_delivery_of_absent_key_is_noop():
    # Delivering a key that was never queued leaves the queue unchanged (caller skips the write).
    pending = {"tasks:def": "m2"}
    assert apply_send_result(pending, "secrets:abc", "m1", True) == {"tasks:def": "m2"}


def test_apply_send_result_does_not_mutate_input():
    pending = {"secrets:abc": "msg"}
    apply_send_result(pending, "secrets:abc", "msg", True)
    assert pending == {"secrets:abc": "msg"}


def test_apply_drain_result_removes_only_delivered():
    pending = {"a:1": "x", "b:2": "y", "c:3": "z"}
    assert apply_drain_result(pending, {"a:1", "c:3"}) == {"b:2": "y"}


def test_apply_drain_result_none_delivered_keeps_all():
    pending = {"a:1": "x", "b:2": "y"}
    assert apply_drain_result(pending, set()) == pending


def test_apply_drain_result_all_delivered_empties():
    assert apply_drain_result({"a:1": "x"}, {"a:1"}) == {}


# behind_marker: the "host is parked on an old tree" signal. Its whole value is the timestamp —
# presence alone is normal (a push is behind for one tick), so these pin the clock semantics.


def test_behind_marker_cleared_when_caught_up():
    assert behind_marker(False, "originX", "originW 100.0", now=200.0) is None


def test_behind_marker_stamps_now_on_first_tick_behind():
    assert behind_marker(True, "originX", None, now=200.0) == "originX 200.0"


def test_behind_marker_keeps_first_seen_across_ticks():
    # Still behind 10 min later: the age must keep growing, not reset.
    assert behind_marker(True, "originX", "originX 200.0", now=800.0) == "originX 200.0"


def test_behind_marker_keeps_first_seen_when_origin_advances():
    # A new push while still stuck refreshes the SHA but must NOT restart the clock — otherwise a
    # steady trickle of pushes to a permanently-stuck host never trips the age threshold.
    assert behind_marker(True, "originZ", "originX 200.0", now=800.0) == "originZ 200.0"


def test_behind_marker_restamps_when_marker_unparseable():
    assert behind_marker(True, "originX", "garbage", now=200.0) == "originX 200.0"


# --- stale-compose watchdog (2nd occurrence of the trap -> machine check) ---------------


def test_declared_services_parses_containers_list_names():
    text = (
        "containers_list:\n"
        "  - name: traefik\n"
        "    port: 8080\n"
        "  - name: monitor-bridge\n"
        "    port: false\n"
        "  # kopia RETIRED 2026-08-10\n"
        "      - name: not-a-service-deeper-indent\n"
    )
    assert declared_services(text) == {"traefik", "monitor-bridge"}


# Platform-aware (2026-08-13): declared_services used to match `- name:` regardless of platform,
# so a platform: k8s entry counted as "declared" here even though deploy.yml's DOCKER play never
# renders a compose for it. A leftover rendered containers/<svc>/ dir for a service that migrated
# to k8s would then phantom-gate as "declared" instead of being flagged stale.
def test_declared_services_skips_platform_k8s_entries():
    text = (
        "containers_list:\n"
        "  - name: crowdsec\n"
        "    platform: k8s\n"
        "    port: 8080\n"
        "  - name: traefik\n"
        "    port: 8080\n"
    )
    assert declared_services(text) == {"traefik"}


def test_declared_services_platform_docker_explicit_is_still_declared():
    text = "containers_list:\n  - name: traefik\n    platform: docker\n    port: 8080\n"
    assert declared_services(text) == {"traefik"}


def test_declared_services_last_entry_platform_k8s_with_no_trailing_entry():
    # The k8s entry is the LAST one in the file (no following `- name:` to bound its block on) —
    # the (?=^  - name: |\Z) lookahead must still terminate the block at EOF.
    text = "containers_list:\n  - name: traefik\n    port: 8080\n  - name: authelia\n    platform: k8s\n    port: 9091\n"
    assert declared_services(text) == {"traefik"}


# --- declared_k8s_services / reroute_k8s_services -----------------------------------------
# A path under ansible/roles/containers/<svc>/{templates,files}/ maps to <svc> by NAME ALONE
# (services_from_changed_paths), with no knowledge of which platform THIS host actually runs
# that service under. wg-easy is a real case: a Docker role (used by daniel-pi), but
# platform: k8s on daniel-box — a template-only push there used to deploy `--tags wg-easy`,
# which resolves to deploy.yml's K8S play, not the Docker one _ACTIVE_CONFIG assumed: an
# idempotent no-op whose health gate is silently skipped too (containers_for() renders no
# compose for a k8s entry). reroute_k8s_services moves such a match into cs.k8s instead, so it
# gets the same defer-and-alert a direct ansible/roles/k8s/** change gets.
def test_declared_k8s_services_parses_platform_k8s_entries():
    text = (
        "containers_list:\n"
        "  - name: wg-easy\n"
        "    platform: k8s\n"
        "    port: 51821\n"
        "  - name: traefik\n"
        "    port: 8080\n"
    )
    assert declared_k8s_services(text) == {"wg-easy"}


def test_declared_k8s_services_excludes_docker_entries():
    text = "containers_list:\n  - name: traefik\n    port: 8080\n"
    assert declared_k8s_services(text) == set()


def test_reroute_k8s_services_moves_matched_service_to_k8s():
    cs = services_from_changed_paths(
        ["ansible/roles/containers/wg-easy/templates/docker-compose.yml.j2"]
    )
    assert cs.services == {"wg-easy"}
    rerouted = reroute_k8s_services(cs, {"wg-easy"})
    assert rerouted.services == set()
    assert rerouted.k8s == {"wg-easy"}


def test_reroute_k8s_services_leaves_docker_services_alone():
    cs = services_from_changed_paths(
        ["ansible/roles/containers/cadvisor/templates/docker-compose.yml.j2"]
    )
    rerouted = reroute_k8s_services(cs, {"wg-easy"})
    assert rerouted.services == {"cadvisor"}
    assert rerouted.k8s == set()


def test_reroute_k8s_services_only_moves_the_matched_subset():
    cs = ChangeSet(services={"cadvisor", "wg-easy"})
    rerouted = reroute_k8s_services(cs, {"wg-easy"})
    assert rerouted.services == {"cadvisor"}
    assert rerouted.k8s == {"wg-easy"}


def test_reroute_k8s_services_merges_into_existing_k8s_set():
    # A single push can carry both a direct ansible/roles/k8s/** change and a containers/<svc>/
    # template for a service that's k8s on this host — both must land in cs.k8s.
    cs = ChangeSet(services={"wg-easy"}, k8s={"authelia"})
    rerouted = reroute_k8s_services(cs, {"wg-easy"})
    assert rerouted.services == set()
    assert rerouted.k8s == {"wg-easy", "authelia"}


def test_stale_rendered_services_flags_only_undeclared_dirs():
    assert stale_rendered_services(
        ["traefik", "kopia", "tempo"], {"traefik", "monitor-bridge"}
    ) == ["kopia", "tempo"]


def test_stale_rendered_services_empty_when_all_declared():
    assert stale_rendered_services(["traefik"], {"traefik", "monitor-bridge"}) == []


# ── k8s auto-deploy: the diff-shape predicate ───────────────────────────────────────────────
_SPEEDTEST_DEFAULTS = "ansible/roles/k8s/speedtest/defaults/main.yml"


def _diff(*lines: str) -> str:
    """A unified diff for _SPEEDTEST_DEFAULTS carrying the given changed lines."""
    header = f"--- a/{_SPEEDTEST_DEFAULTS}\n+++ b/{_SPEEDTEST_DEFAULTS}\n@@ -2 +2 @@\n"
    return header + "".join(line + "\n" for line in lines)


def test_image_only_diff_accepts_a_pure_image_bump():
    assert is_image_only_diff(
        _diff(
            "-speedtest_k8s_image: openspeedtest/latest:v2.0.4",
            "+speedtest_k8s_image: openspeedtest/latest:v2.0.5",
        )
    )


def test_image_only_diff_rejects_a_bundled_non_image_line():
    assert not is_image_only_diff(
        _diff(
            "-speedtest_k8s_image: openspeedtest/latest:v2.0.4",
            "+speedtest_k8s_image: openspeedtest/latest:v2.0.5",
            "-speedtest_k8s_replicas: 1",
            "+speedtest_k8s_replicas: 2",
        )
    )


def test_image_only_diff_ignores_file_headers_not_content():
    # `--- a/...` / `+++ b/...` start with -/+ but are metadata, not changed lines.
    assert is_image_only_diff(
        _diff("-speedtest_k8s_image: a:1", "+speedtest_k8s_image: a:2")
    )


def test_image_only_diff_rejects_an_empty_diff():
    # Nothing to prove -> fail closed, so an unreadable/empty git diff defers.
    assert not is_image_only_diff("")


def test_image_only_diff_rejects_a_header_only_diff():
    assert not is_image_only_diff(
        f"--- a/{_SPEEDTEST_DEFAULTS}\n+++ b/{_SPEEDTEST_DEFAULTS}\n"
    )


def test_image_only_diff_rejects_a_commented_out_image_line():
    assert not is_image_only_diff(
        _diff("-# speedtest_k8s_image: a:1", "+# speedtest_k8s_image: a:2")
    )


def test_image_only_diff_accepts_a_digest_bump():
    # The 18 mutable-tag digest pins are the population the digest automerge rule targets.
    assert is_image_only_diff(
        _diff(
            "-littlelink_k8s_image: littlelink:latest@sha256:aaa",
            "+littlelink_k8s_image: littlelink:latest@sha256:bbb",
        )
    )


# ── k8s auto-deploy: the eligibility split ──────────────────────────────────────────────────
def _split(
    paths,
    *,
    denylist=frozenset(),
    pilot=frozenset(),
    enabled=True,
    image_only=True,
    max_per_tick=0,
):
    cs = services_from_changed_paths(paths)
    return split_k8s_auto_deploy(
        cs,
        paths,
        denylist=denylist,
        pilot=pilot,
        enabled=enabled,
        image_only=lambda _svc: image_only,
        max_per_tick=max_per_tick,
    )


def test_split_k8s_promotes_an_image_only_bump():
    cs = _split([_SPEEDTEST_DEFAULTS], denylist={"traefik"})
    assert cs.k8s_deploy == {"speedtest"}
    assert cs.k8s == set()


def test_split_k8s_disabled_reproduces_todays_behaviour_exactly():
    cs = _split([_SPEEDTEST_DEFAULTS], denylist={"traefik"}, enabled=False)
    assert cs.k8s_deploy == set()
    assert cs.k8s == {"speedtest"}


def _defaults_for(svc):
    return f"ansible/roles/k8s/{svc}/defaults/main.yml"


def test_split_k8s_caps_how_many_services_one_tick_takes_on():
    # The promoted set shares ONE ansible-playbook run and one K8S_DEPLOY_TIMEOUT_S, and a
    # timeout git-resets the whole merged range — so an uncapped tick can discard four good
    # image bumps because the fifth failed to roll out.
    paths = [_defaults_for(s) for s in ("speedtest", "freshrss", "sonarr", "radarr")]
    cs = _split(paths, max_per_tick=2)
    assert len(cs.k8s_deploy) == 2
    # The surplus stays in cs.k8s, which defer-and-alerts — so it reaches the operator as a
    # Discord message naming the services to deploy by hand. It is NOT retried automatically:
    # the ff-merge precedes the deploy, so the next tick sees local == origin and noops. This
    # assertion covers the partition only; nothing here should be read as a retry guarantee.
    assert cs.k8s == {"speedtest", "freshrss", "sonarr", "radarr"} - cs.k8s_deploy


def test_split_k8s_cap_is_deterministic():
    # Same input, same promotion — otherwise which bumps land depends on set iteration order.
    paths = [_defaults_for(s) for s in ("speedtest", "freshrss", "sonarr", "radarr")]
    assert _split(paths, max_per_tick=2).k8s_deploy == (
        _split(paths, max_per_tick=2).k8s_deploy
    )


def test_split_k8s_cap_of_zero_promotes_everything_eligible():
    paths = [_defaults_for(s) for s in ("speedtest", "freshrss")]
    assert _split(paths, max_per_tick=0).k8s_deploy == {"speedtest", "freshrss"}


def test_split_k8s_never_promotes_a_denylisted_service():
    cs = _split([_SPEEDTEST_DEFAULTS], denylist={"speedtest"})
    assert cs.k8s_deploy == set()
    assert cs.k8s == {"speedtest"}


def test_split_k8s_rejects_a_non_image_diff():
    cs = _split([_SPEEDTEST_DEFAULTS], denylist={"traefik"}, image_only=False)
    assert cs.k8s_deploy == set()
    assert cs.k8s == {"speedtest"}


def test_split_k8s_blocks_a_service_with_a_second_changed_path():
    # Clean image bump, but the same push also edits the role's tasks/ — deploying would apply
    # an unsoaked structural change alongside it.
    cs = _split(
        [_SPEEDTEST_DEFAULTS, "ansible/roles/k8s/speedtest/tasks/main.yml"],
        denylist={"traefik"},
    )
    assert cs.k8s_deploy == set()
    assert cs.k8s == {"speedtest"}


def test_split_k8s_pilot_scope_restricts_eligibility():
    paths = [_SPEEDTEST_DEFAULTS, "ansible/roles/k8s/littlelink/defaults/main.yml"]
    cs = _split(paths, denylist={"traefik"}, pilot={"speedtest"})
    assert cs.k8s_deploy == {"speedtest"}
    assert cs.k8s == {"littlelink"}


def test_split_k8s_empty_pilot_means_the_denylist_governs():
    # Slice 3 (2026-08-16) cleared the pilot list. An empty pilot must mean "everything not
    # denylisted", never "nothing" — the opposite reading of the same falsy value, and the one
    # that would silently disarm the feature instead of widening it.
    paths = [_SPEEDTEST_DEFAULTS, _defaults_for("littlelink"), _defaults_for("sonarr")]
    cs = _split(paths, denylist={"sonarr"}, pilot=frozenset())
    assert cs.k8s_deploy == {"speedtest", "littlelink"}
    assert cs.k8s == {"sonarr"}


def test_split_k8s_denies_the_services_the_pilot_used_to_mask():
    # These six sat outside the denylist only because the pilot named neither them nor anything
    # else; each matches an exclusion class the design already publishes. Clearing the pilot
    # without adding them would have armed all six at once.
    masked = ("qbittorrent", "bazarr", "tdarr", "livesync", "valheim", "valheim-stats")
    cs = _split([_defaults_for(s) for s in masked], denylist=set(masked))
    assert cs.k8s_deploy == set()
    assert cs.k8s == set(masked)


def test_split_k8s_defers_when_the_tick_also_carries_docker_services():
    # main()'s k8s branch returns before the Docker deploy + health gate, so promoting here
    # would silently skip them. Defer instead.
    paths = [
        _SPEEDTEST_DEFAULTS,
        "ansible/roles/containers/dozzle/templates/docker-compose.yml.j2",
    ]
    cs = _split(paths, denylist={"traefik"})
    assert cs.k8s_deploy == set()
    assert cs.k8s == {"speedtest"}
    assert cs.services == {"dozzle"}


def test_split_k8s_combined_push_deploys_eligible_defers_denylisted():
    paths = [_SPEEDTEST_DEFAULTS, "ansible/roles/k8s/traefik/defaults/main.yml"]
    cs = _split(paths, denylist={"traefik"})
    assert cs.k8s_deploy == {"speedtest"}
    assert cs.k8s == {"traefik"}


# ── CI gate ───────────────────────────────────────────────────────────────────────────────────

_PREK = "prek (lint + validate + tests + secrets)"
_REQUIRED = frozenset({_PREK})


def _run(name, status="completed", conclusion="success"):
    return {"name": name, "status": status, "conclusion": conclusion}


def test_ci_verdict_passes_when_required_context_is_green():
    assert ci_verdict([_run(_PREK)], _REQUIRED) == "pass"


def test_ci_verdict_fails_on_failure():
    assert ci_verdict([_run(_PREK, conclusion="failure")], _REQUIRED) == "fail"
    assert ci_verdict([_run(_PREK, conclusion="timed_out")], _REQUIRED) == "fail"


def test_ci_verdict_pending_while_still_running():
    assert ci_verdict(
        [_run(_PREK, status="in_progress", conclusion=None)], _REQUIRED
    ) == ("pending")
    assert (
        ci_verdict([_run(_PREK, status="queued", conclusion=None)], _REQUIRED)
        == "pending"
    )


def test_ci_verdict_pending_when_the_context_has_not_reported_at_all():
    # A SHA pushed seconds ago has no check-runs yet. Absence must never read as success.
    assert ci_verdict([], _REQUIRED) == "pending"
    assert ci_verdict([_run("some other job")], _REQUIRED) == "pending"


def test_ci_verdict_treats_cancelled_as_no_verdict_not_failure():
    # ci.yml sets concurrency cancel-in-progress on github.ref, so two pushes in quick succession
    # CANCEL the first run. That means "no verdict for this SHA", not "this SHA is bad" — mapping
    # it to a failure would page on an ordinary back-to-back push.
    assert ci_verdict([_run(_PREK, conclusion="cancelled")], _REQUIRED) == "pending"
    assert ci_verdict([_run(_PREK, conclusion="stale")], _REQUIRED) == "pending"


def test_ci_verdict_skipped_and_neutral_count_as_green():
    assert ci_verdict([_run(_PREK, conclusion="skipped")], _REQUIRED) == "pass"
    assert ci_verdict([_run(_PREK, conclusion="neutral")], _REQUIRED) == "pass"


def test_ci_verdict_failure_wins_over_a_second_run_of_the_same_name():
    # One name can carry several runs (a re-run, or push + pull_request on the same SHA).
    # The worst outcome has to win, or a green re-run would paper over a red one.
    runs = [_run(_PREK), _run(_PREK, conclusion="failure")]
    assert ci_verdict(runs, _REQUIRED) == "fail"
    assert ci_verdict(list(reversed(runs)), _REQUIRED) == "fail"


def test_ci_verdict_pending_when_one_run_of_the_name_is_unfinished():
    runs = [_run(_PREK), _run(_PREK, status="in_progress", conclusion=None)]
    assert ci_verdict(runs, _REQUIRED) == "pending"


def test_ci_verdict_all_of_several_required_contexts_must_be_green():
    required = frozenset({_PREK, "renovate config validator"})
    assert ci_verdict([_run(_PREK)], required) == "pending"
    assert (
        ci_verdict([_run(_PREK), _run("renovate config validator")], required) == "pass"
    )


def test_ci_verdict_empty_required_set_disarms_the_gate():
    # An un-templated config.env leaves CI_CONTEXTS empty; that host must keep its old behaviour
    # rather than deferring every tick forever.
    assert ci_verdict([], frozenset()) == "pass"
    assert ci_verdict([_run(_PREK, conclusion="failure")], frozenset()) == "pass"


def test_next_action_defers_when_ci_has_not_finished():
    assert next_action("aaa", "bbb", None, ci="pending") == "ci_pending"


def test_next_action_refuses_to_deploy_a_red_tip():
    assert next_action("aaa", "bbb", None, ci="fail") == "ci_failed"


def test_next_action_deploys_when_ci_is_green():
    assert next_action("aaa", "bbb", None, ci="pass") == "deploy"


def test_next_action_defaults_to_deploying_when_no_ci_verdict_is_supplied():
    # Back-compat: every existing caller and test omits `ci`, and must still deploy.
    assert next_action("aaa", "bbb", None) == "deploy"


def test_ci_never_overrides_the_earlier_short_circuits():
    # dirty / noop / skip_hold all outrank the CI gate: a red tip we were never going to deploy
    # must not start reporting itself as a CI failure.
    assert next_action("aaa", "bbb", None, dirty=True, ci="fail") == "dirty"
    assert next_action("aaa", "aaa", None, ci="fail") == "noop"
    assert next_action("aaa", "bad", "bad", ci="fail") == "skip_hold"
    assert next_action("aaa", "bbb", None, origin_ahead=False, ci="fail") == "noop"


def test_declared_denylist_collects_roles_declaring_false():
    sources = {
        "sonarr": 'k8s_autodeploy: false\nk8s_autodeploy_reason: "x"\n',
        "homepage": 'k8s_autodeploy: true\nk8s_autodeploy_reason: "y"\n',
    }
    assert declared_denylist(sources) == frozenset({"sonarr"})


def test_declared_denylist_ignores_the_shared_roles():
    sources = {name: None for name in SHARED_K8S_ROLES}
    sources["sonarr"] = "k8s_autodeploy: false\n"
    assert declared_denylist(sources) == frozenset({"sonarr"})


def test_an_unparseable_declaration_counts_as_denied():
    """Fail closed: a role we cannot read must not silently match the config's view."""
    sources = {"weird": "k8s_autodeploy: maybe\n", "ok": "k8s_autodeploy: true\n"}
    assert declared_denylist(sources) == frozenset({"weird"})


def test_a_missing_declaration_counts_as_denied():
    sources = {"silent": "some_other_var: 1\n", "ok": "k8s_autodeploy: true\n"}
    assert declared_denylist(sources) == frozenset({"silent"})


def test_a_missing_defaults_file_counts_as_denied():
    sources = {"gone": None, "ok": "k8s_autodeploy: true\n"}
    assert declared_denylist(sources) == frozenset({"gone"})


def test_a_trailing_comment_does_not_break_parsing():
    sources = {"r": "k8s_autodeploy: false  # noqa var-naming[no-role-prefix]\n"}
    assert declared_denylist(sources) == frozenset({"r"})


def test_yaml_no_and_off_read_as_false():
    """PyYAML resolves these to False, so the filter denies them; match that."""
    assert declared_denylist({"a": "k8s_autodeploy: no\n"}) == frozenset({"a"})
    assert declared_denylist({"b": "k8s_autodeploy: off\n"}) == frozenset({"b"})


def test_an_indented_key_is_not_a_declaration():
    """Only a top-level key declares. An indented one belongs to some other block."""
    sources = {"r": "something:\n  k8s_autodeploy: true\n"}
    assert declared_denylist(sources) == frozenset(
        {"r"}
    )  # denied: no top-level declaration


def test_a_duplicate_key_with_the_denial_first_reads_as_denied():
    sources = {"r": "k8s_autodeploy: false\nk8s_autodeploy: true\n"}
    assert declared_denylist(sources) == frozenset({"r"})


def test_a_duplicate_key_with_the_denial_last_reads_as_denied():
    # YAML itself is last-key-wins, so a real parser would call this permitted. This reader
    # requires unanimity instead, which is strictly more conservative — see declared_denylist's
    # docstring for why that's the safe direction to diverge in.
    sources = {"r": "k8s_autodeploy: true\nk8s_autodeploy: false\n"}
    assert declared_denylist(sources) == frozenset({"r"})


def test_no_space_after_the_colon_is_not_a_declaration():
    sources = {"r": "k8s_autodeploy:true\n"}
    assert declared_denylist(sources) == frozenset({"r"})


def test_a_decoy_declaration_inside_a_quoted_scalar_reads_as_denied():
    # A multi-line quoted YAML scalar can contain a line that starts at column 0 and looks
    # exactly like a top-level `k8s_autodeploy: true` declaration, even though it's just text
    # inside some other key's value. The regex can't tell the difference — it isn't a YAML
    # parser — so it must not let that decoy line outvote the real, later `false`.
    sources = {
        "r": (
            'k8s_autodeploy_reason: "line one\n'
            "k8s_autodeploy: true\n"
            'still inside the quote"\n'
            "k8s_autodeploy: false\n"
        )
    }
    assert declared_denylist(sources) == frozenset({"r"})


def test_crlf_line_endings_are_not_recognized_and_so_deny():
    sources = {"r": "k8s_autodeploy: true\r\n"}
    assert declared_denylist(sources) == frozenset({"r"})


def test_declares_snapshot_claims_true_for_a_single_claim():
    assert declares_snapshot_claims(
        "k8s_autodeploy_snapshot_pvcs: [bazarr-config]  # noqa\n"
    )


def test_declares_snapshot_claims_true_for_a_two_claim_role():
    assert declares_snapshot_claims(
        "k8s_autodeploy_snapshot_pvcs: [tdarr-configs, tdarr-server]  # noqa\n"
    )


def test_declares_snapshot_claims_false_for_an_empty_list():
    assert not declares_snapshot_claims("k8s_autodeploy_snapshot_pvcs: []\n")


def test_declares_snapshot_claims_false_for_an_absent_key():
    assert not declares_snapshot_claims("some_other_var: 1\n")


def test_declares_snapshot_claims_false_for_none():
    assert not declares_snapshot_claims(None)


def test_declares_snapshot_claims_false_for_empty_string():
    assert not declares_snapshot_claims("")


def test_declares_snapshot_claims_ignores_an_indented_key():
    assert not declares_snapshot_claims(
        "something:\n  k8s_autodeploy_snapshot_pvcs: [x]\n"
    )


# declares_snapshot_claims() is a regex over source text, used only to word gitops-deploy's
# rollback alert. roles/k8s/manifests decides the REAL revert from `yaml.safe_load`'d defaults —
# a different reader of the same file. All 13 roles that declare
# `k8s_autodeploy_snapshot_pvcs` today write it as a single-line list literal, so nothing has
# ever exercised the gap: reformat one to block style and the regex returns False (no revert
# applies, says the alert) while the volume still reverts for real. This walks every role's
# actual defaults/main.yml and pins that the two readers agree on all of them, so a future
# reformat fails this test instead of surfacing as an incident alert that names the wrong thing.
_K8S_ROLES_DIR = pathlib.Path(__file__).parents[3] / "k8s"


def _yaml_declares_claims(text: str) -> bool:
    data = yaml.safe_load(text) or {}
    return bool(data.get("k8s_autodeploy_snapshot_pvcs"))


def test_declares_snapshot_claims_agrees_with_yaml_for_every_k8s_role():
    mismatches = []
    for defaults_path in sorted(_K8S_ROLES_DIR.glob("*/defaults/main.yml")):
        text = defaults_path.read_text()
        regex_verdict = declares_snapshot_claims(text)
        yaml_verdict = _yaml_declares_claims(text)
        if regex_verdict != yaml_verdict:
            mismatches.append(
                f"{defaults_path.relative_to(_K8S_ROLES_DIR.parent)}: "
                f"regex={regex_verdict} yaml={yaml_verdict}"
            )
    assert not mismatches, (
        "declares_snapshot_claims()'s regex disagrees with what roles/k8s/manifests actually "
        "reads via yaml.safe_load for:\n" + "\n".join(mismatches)
    )


def test_rollback_volume_revert_note_reports_the_redeploy_failure_when_it_failed():
    """The redeploy raising means the revert task inside roles/k8s/manifests may never have
    run — the note must say so, not claim a revert was attempted."""
    note = rollback_volume_revert_note({"sonarr"}, frozenset(), "boom")
    assert "rollback redeploy itself failed" in note
    assert "boom" in note
    assert "Volume revert" not in note


def test_rollback_volume_revert_note_says_no_claims_when_none_declare():
    note = rollback_volume_revert_note({"speedtest"}, frozenset(), None)
    assert "No service" in note
    assert "no volume revert applies" in note


def test_rollback_volume_revert_note_names_only_the_services_that_revert():
    note = rollback_volume_revert_note(
        {"sonarr", "speedtest"}, frozenset({"sonarr"}), None
    )
    assert "`sonarr`" in note
    assert "speedtest" in note  # named as unaffected, not silently dropped
    assert "declares no `k8s_autodeploy_snapshot_pvcs` and is unaffected" in note


def test_rollback_volume_revert_note_omits_the_unaffected_aside_when_all_revert():
    note = rollback_volume_revert_note({"sonarr"}, frozenset({"sonarr"}), None)
    assert "unaffected" not in note


def test_k8s_role_paths_finds_a_normal_role():
    listing = "ansible/roles/k8s/sonarr/defaults/main.yml\nansible/roles/k8s/sonarr/tasks/main.yml\n"
    assert k8s_role_paths(listing) == {
        "sonarr": "ansible/roles/k8s/sonarr/defaults/main.yml"
    }


def test_k8s_role_paths_a_role_with_no_defaults_maps_to_none():
    listing = "ansible/roles/k8s/homepage/tasks/main.yml\n"
    assert k8s_role_paths(listing) == {"homepage": None}


def test_k8s_role_paths_a_defaults_dir_holding_something_other_than_main_yml():
    listing = "ansible/roles/k8s/sonarr/defaults/other.yml\n"
    assert k8s_role_paths(listing) == {"sonarr": None}


def test_k8s_role_paths_ignores_a_stray_file_directly_under_roles_k8s():
    listing = "ansible/roles/k8s/README.md\n"
    assert k8s_role_paths(listing) == {}


def test_k8s_role_paths_empty_listing():
    assert k8s_role_paths("") == {}


def test_k8s_role_paths_order_does_not_matter():
    before_after = (
        "ansible/roles/k8s/sonarr/tasks/main.yml\n"
        "ansible/roles/k8s/sonarr/defaults/main.yml\n"
        "ansible/roles/k8s/sonarr/templates/deployment.yaml.j2\n"
    )
    after_before = (
        "ansible/roles/k8s/sonarr/templates/deployment.yaml.j2\n"
        "ansible/roles/k8s/sonarr/defaults/main.yml\n"
        "ansible/roles/k8s/sonarr/tasks/main.yml\n"
    )
    expected = {"sonarr": "ansible/roles/k8s/sonarr/defaults/main.yml"}
    assert k8s_role_paths(before_after) == expected
    assert k8s_role_paths(after_before) == expected


# ── deploy_k8s ────────────────────────────────────────────────────────────────────────────────
# gitops_deploy.py reads /etc/gitops-deploy/config.env at import time (`C = cfg()`), which
# doesn't exist in CI — see test_gitops_discord_contract.py's docstring, which is why every other
# guard on that module is an AST source check rather than an import. Stub host_lib.parse_env_file
# with canned values BEFORE the only import of gitops_deploy in this file, so the import behaves
# identically in CI and on a host where the real config.env exists, and this suite never reads
# the real secrets file (forbidden — it's SOPS-managed, see the role CLAUDE.md).
def _import_gitops_deploy():
    import host_lib

    real_parse_env_file = host_lib.parse_env_file
    host_lib.parse_env_file = lambda _path: {
        "REPO_DIR": "/tmp/gitops-test-repo",
        "HOSTNAME": "test-host",
        "DISCORD_WEBHOOK": "https://discord.example/webhook",
    }
    try:
        sys.modules.pop("gitops_deploy", None)
        import gitops_deploy
    finally:
        host_lib.parse_env_file = real_parse_env_file
    return gitops_deploy


gitops_deploy = _import_gitops_deploy()


def _capture_run(monkeypatch):
    """Patch gitops_deploy.run() to record every call instead of shelling out, and return the
    list it appends to."""

    class _Call:
        def __init__(self, argv, kwargs):
            self.argv = argv
            self.kwargs = kwargs

    calls: list[_Call] = []

    def _fake_run(argv, **kwargs):
        calls.append(_Call(argv, kwargs))
        return ""

    monkeypatch.setattr(gitops_deploy, "run", _fake_run)
    return calls


_FORWARD_ARGV = [
    "uv",
    "run",
    "--frozen",
    "ansible-playbook",
    "ansible/deploy.yml",
    "--tags",
    "sonarr",
]


def test_deploy_k8s_passes_no_extra_vars_by_default(monkeypatch) -> None:
    """The ordinary deploy must be byte-identical to what it was before this slice. ~50
    services go through this call on every tick. Pins the full argv, not just -e's absence —
    a stray extra arg anywhere else in the list would pass a presence-only check."""
    calls = _capture_run(monkeypatch)
    gitops_deploy.deploy_k8s({"sonarr"}, 900.0)
    assert calls[0].argv == _FORWARD_ARGV


def test_deploy_k8s_passes_the_restore_sha_when_given(monkeypatch) -> None:
    calls = _capture_run(monkeypatch)
    gitops_deploy.deploy_k8s({"sonarr"}, 900.0, restore_sha="deadbeef")
    assert calls[0].argv == _FORWARD_ARGV + ["-e", "k8s_restore_snapshot_sha=deadbeef"]


def test_deploy_k8s_treats_a_whitespace_only_restore_sha_as_absent(monkeypatch) -> None:
    """restore_sha="" or all-whitespace must stay inert, matching the manifests role's own
    `| trim | length > 0` guard — a blank-but-truthy string must not add a broken `-e` arg."""
    calls = _capture_run(monkeypatch)
    gitops_deploy.deploy_k8s({"sonarr"}, 900.0, restore_sha="   ")
    assert calls[0].argv == _FORWARD_ARGV


# ── the rollback call site in main() ─────────────────────────────────────────────────────────
# main() shells out to git, queries GitHub for a CI verdict over HTTP, posts to Discord, and
# touches several state files under /var/lib/gitops-deploy — exercising it end-to-end would mean
# mocking all of that for one two-line assertion. This parses the ACTUAL call arguments Python
# executes (not comment text), the same AST-source-guard shape test_gitops_discord_contract.py
# already uses for the rest of this un-importable-in-CI module.
_GITOPS_SRC = pathlib.Path(__file__).with_name("gitops_deploy.py")


def _deploy_k8s_calls_in_main() -> list[ast.Call]:
    tree = ast.parse(_GITOPS_SRC.read_text())
    main_fn = next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "main"
    )
    return [
        n
        for n in ast.walk(main_fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "deploy_k8s"
    ]


def test_the_rollback_redeploy_passes_the_FAILED_sha_not_the_good_one():
    """The snapshot worth reverting to was taken before the failed deploy, so it is named for
    `origin` — the commit being rolled back FROM. Passing `local` here would look correct, find
    no snapshot for a first-time rollback, and fail the deploy; worse, on a second rollback of
    the same service it would find a stale snapshot and revert to the wrong point."""
    calls = _deploy_k8s_calls_in_main()
    assert len(calls) == 2, (
        "expected exactly one forward deploy_k8s call and one rollback redeploy in main()"
    )
    forward, rollback = calls

    forward_kwargs = {kw.arg for kw in forward.keywords}
    assert "restore_sha" not in forward_kwargs, (
        "the forward deploy must not pass restore_sha"
    )

    rollback_kwargs = {kw.arg: kw.value for kw in rollback.keywords}
    assert "restore_sha" in rollback_kwargs, (
        "the rollback redeploy must pass restore_sha"
    )
    # Pin the exact expression, not a prefix: `startswith("origin")` alone is satisfied by the
    # full 40-char `origin` (volume-snapshot names with `git rev-parse --short=8`, so a 40-char
    # SHA matches no snapshot and the revert silently never runs) and by an unrelated
    # `origin_decoy` variable — neither is what this call site must send.
    sha_expr = ast.unparse(rollback_kwargs["restore_sha"])
    assert sha_expr == "origin[:8]", (
        f"rollback restore_sha must be exactly `origin[:8]` — the FAILED commit's short SHA, "
        f"matching how volume-snapshot names its snapshots — got `{sha_expr}`"
    )


def test_the_rollback_redeploy_uses_its_own_timeout_budget():
    """Task 4's addendum: give the rollback redeploy a distinct budget rather than sharing
    K8S_DEPLOY_TIMEOUT_S, since it does strictly more work than the forward deploy."""
    forward, rollback = _deploy_k8s_calls_in_main()
    assert ast.unparse(forward.args[1]) == "K8S_DEPLOY_TIMEOUT_S"
    assert ast.unparse(rollback.args[1]) == "K8S_ROLLBACK_TIMEOUT_S"


# ── run()'s timeout must kill the whole process group ───────────────────────────────────────────
# `uv run ansible-playbook ...` is a GRANDCHILD of run()'s subprocess (uv forks it rather than
# exec'ing into it). `subprocess.run(timeout=)` DOES return promptly on timeout — its internal
# communicate() raises on the wall-clock deadline, not on pipe EOF — but it kills only the DIRECT
# child (uv). Verified empirically against the pre-fix implementation: the call returns on time
# and the grandchild is still alive at that moment, left running as an orphan with nothing
# watching it. That is how K8S_ROLLBACK_TIMEOUT_S stopped being an actual bound on the underlying
# ansible-playbook: gitops_deploy.py moves on while the timed-out run keeps mutating the cluster,
# and the real stop becomes systemd's TimeoutStartSec SIGTERM against the wrapping unit, which can
# land mid-rollback. This shape reproduces it directly: a shell script backgrounds a grandchild
# that outlives a naive kill-the-direct-child-only fix, so the test fails against the OLD run()
# and passes only once the whole process group is killed.
_GRANDCHILD_SHAPE = """#!/bin/sh
sh -c 'echo $$ > "{pidfile}"; sleep 30' &
wait
"""


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_run_timeout_kills_the_whole_process_group(tmp_path) -> None:
    pidfile = tmp_path / "grandchild.pid"
    script = tmp_path / "parent.sh"
    script.write_text(_GRANDCHILD_SHAPE.format(pidfile=pidfile))
    script.chmod(0o755)

    start = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        gitops_deploy.run(["sh", str(script)], cwd=str(tmp_path), timeout=1.0)
    elapsed = time.monotonic() - start

    # Both the buggy and the fixed run() return around the 1.0s deadline — the deadline is a
    # wall-clock check inside communicate(), not a wait for pipe EOF, so this alone does not
    # discriminate them. It is here as a sanity bound; the real regression check is the
    # grandchild-liveness assert below.
    assert elapsed < 10, (
        f"run() took {elapsed:.1f}s to return after a 1.0s timeout — expected it to return "
        f"around the deadline regardless of whether the fix is applied"
    )

    deadline = time.monotonic() + 2
    grandchild_pid = None
    while grandchild_pid is None and time.monotonic() < deadline:
        if pidfile.exists():
            grandchild_pid = int(pidfile.read_text().strip())
        else:
            time.sleep(0.05)
    assert grandchild_pid is not None, "the grandchild never started"

    # SIGKILL is instant but reaping is not: once its own parent (the script) is also killed,
    # the grandchild is reparented and reaped by the nearest subreaper — poll briefly instead
    # of asserting the instant killpg returns.
    deadline = time.monotonic() + 3
    while _pid_is_alive(grandchild_pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _pid_is_alive(grandchild_pid), (
        f"grandchild pid {grandchild_pid} outlived the timeout — only the direct child was "
        f"killed, not its process group"
    )
