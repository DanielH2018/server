# ansible/roles/setup/gitops_deploy/files/test_deploy_logic.py
from datetime import datetime

from deploy_logic import (
    services_from_changed_paths,
    next_action,
    container_names,
    containers_to_gate,
    should_alert_dirty,
    health_decision,
    health_settles,
    gate_services,
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
    for p in ("ansible/initial_setup.yml", "ansible/bootstrap.yml"):
        cs = services_from_changed_paths([p])
        assert cs.broad is True, p
        assert cs.services == set()


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
    cs = services_from_changed_paths(
        ["ansible/roles/containers/monitor-bridge/files/check.py"]
    )
    assert cs.services == {"monitor-bridge"}
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
# most once per America/Chicago calendar day, and never before the morning hour
# (07:00 CT) — so an overnight-dirty tree pages once at ~7 AM, not all night.
def test_dirty_alert_fires_first_tick_after_7am_when_never_alerted():
    # Overnight-dirty tree, first eligible morning tick, no prior alert today.
    now = datetime(2026, 6, 20, 7, 0)
    assert should_alert_dirty(now, None) is True


def test_dirty_alert_suppressed_before_7am():
    # A pre-dawn tick must stay silent even if we've never alerted.
    now = datetime(2026, 6, 20, 6, 59)
    assert should_alert_dirty(now, None) is False


def test_dirty_alert_suppressed_when_already_alerted_today():
    # Second (and every later) tick on the same CT day after the morning alert.
    now = datetime(2026, 6, 20, 12, 30)
    assert should_alert_dirty(now, "2026-06-20") is False


def test_dirty_alert_fires_again_on_a_new_day():
    # Still dirty the next morning -> a fresh once-a-day reminder.
    now = datetime(2026, 6, 21, 7, 15)
    assert should_alert_dirty(now, "2026-06-20") is True


def test_dirty_alert_at_exactly_7am_boundary_inclusive():
    now = datetime(2026, 6, 20, 7, 0)
    assert should_alert_dirty(now, "2026-06-19") is True


def test_dirty_alert_newly_dirtied_after_7am_alerts_once():
    # Tree goes dirty mid-afternoon with no alert recorded today -> one alert now.
    now = datetime(2026, 6, 20, 15, 0)
    assert should_alert_dirty(now, None) is True


def test_dirty_alert_custom_hour():
    assert should_alert_dirty(datetime(2026, 6, 20, 8, 0), None, alert_hour=9) is False
    assert should_alert_dirty(datetime(2026, 6, 20, 9, 0), None, alert_hour=9) is True


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
