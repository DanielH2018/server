"""Gating a Docker deploy on health, and getting the bad news out.

A container is healthy only once it has stayed that way, so the streak logic is what separates
"came up" from "came up and stayed". `gate_services` spends a shared deadline across services,
which is why exhausting it midway must fail the rest rather than pass them by default. The
gate inspects every `container_name:` in the changed service's rendered compose, since a role
often runs several containers and the bumped image's container is usually not the role-named
one -- and a service with no compose on this host is skipped, not polled to the deadline.

The Discord queue is the other half: an alert that fails to send has to survive to the next
tick, and the queue has to stay bounded.
"""

# ansible/roles/setup/gitops_deploy/files/test_deploy_health.py

from deploy_health import (
    PENDING_ALERTS_MAX,
    apply_drain_result,
    apply_send_result,
    cap_pending,
    container_names,
    containers_to_gate,
    gate_services,
    health_decision,
    health_settles,
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


# ── the pending-alert queue is bounded ─────────────────────────────────────────────────────────


def test_cap_pending_leaves_a_queue_under_the_cap_alone():
    """The accepting half. A cap that trimmed unconditionally would pass the rejecting test
    below while quietly discarding alerts that fit."""
    queue = {f"secrets:{i}": "msg" for i in range(5)}
    capped, dropped = cap_pending(dict(queue), max_entries=64)
    assert capped == queue
    assert dropped == []


def test_cap_pending_evicts_the_oldest_first():
    """The rejecting half. Dicts preserve insertion order and json.load preserves it on the way
    back in, so the queue's own order IS its age order — no timestamps needed."""
    queue = {f"secrets:{i}": f"msg{i}" for i in range(6)}
    capped, dropped = cap_pending(queue, max_entries=4)
    assert dropped == ["secrets:0", "secrets:1"]
    assert list(capped) == ["secrets:2", "secrets:3", "secrets:4", "secrets:5"]
    assert capped["secrets:5"] == "msg5"


def test_cap_pending_reports_every_key_it_drops():
    """Dropping an undelivered alert without a trace is the failure the queue exists to prevent,
    one level up — so the caller must be able to log each one."""
    queue = {f"secrets:{i}": "msg" for i in range(10)}
    capped, dropped = cap_pending(queue, max_entries=3)
    assert len(dropped) == 7
    assert set(dropped).isdisjoint(capped)
    assert len(capped) == 3


def test_cap_pending_at_exactly_the_cap_drops_nothing():
    """The off-by-one that would silently discard one alert per tick at steady state."""
    queue = {f"secrets:{i}": "msg" for i in range(4)}
    capped, dropped = cap_pending(queue, max_entries=4)
    assert dropped == []
    assert capped == queue


def test_the_default_cap_is_the_one_the_deployer_uses():
    """Binds the default to the exported constant, so raising one raises both."""
    queue = {f"secrets:{i}": "msg" for i in range(PENDING_ALERTS_MAX + 2)}
    _capped, dropped = cap_pending(queue)
    assert len(dropped) == 2
