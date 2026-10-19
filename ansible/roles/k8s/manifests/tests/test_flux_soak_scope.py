"""The soak Job script's alert leg, scope, wait length, connection and revision check.

Each case is D5's step 16 (D5.3b1), with the red half the plan names. `_soak_world.World` is
the promotion every case starts from.

Run: uv run pytest ansible/roles/k8s/manifests/tests/test_flux_soak_scope.py
"""

import http.server
import json
import socket
import threading

import pytest

import flux_soak
from _soak_world import OTHER, R, T0, W, World, alert, iso, run
from renderer.renderer_lib.ordering import (
    DEFERRED_SUFFIX,
    PRECHECK_SUFFIX,
    PREPULL_SUFFIX,
    SNAPSHOT_SUFFIX,
    VERIFY_SUFFIX,
)


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setenv("HOSTNAME", "soak-abc-x1")
    return World()


# Step 16: alerts.


@pytest.mark.parametrize(
    ("labels", "active", "fails"),
    [
        (alert(), T0 - 60, False),
        (alert(), T0 + 60, True),
        (alert(severity="warning", verify="true"), T0 + 60, True),
        (alert(shadow="true"), T0 + 60, False),
        (alert(alertname="Watchdog"), T0 + 60, False),
    ],
    ids=[
        "critical-before-t0",
        "critical-after-t0",
        "verify-warning",
        "shadow",
        "watchdog",
    ],
)
def test_an_alert_fails_only_when_critical_or_verify_and_active_after_t0(
    world, tmp_path, labels, active, fails
):
    world.alerts = [(labels, active)]
    assert run(world, tmp_path)[0] == int(fails)


def test_a_critical_alert_still_pending_after_t0_passes(world, tmp_path):
    # The red half: ALERTS_FOR_STATE alone lists a pending alert beside the firing ones.
    world.pending_alerts = [(alert(), T0 + 60)]
    assert run(world, tmp_path) == (0, "")


@pytest.mark.parametrize(("restarted", "code"), [(T0 + 200, 0), (T0 - 600, 1)])
def test_a_no_for_alert_reactivated_by_a_prometheus_restart_after_t0_passes(
    world, tmp_path, restarted, code
):
    # The red half: with Prometheus up since before T0, the same activeAt is a new alert.
    world.prometheus_started = restarted
    world.alerts = [(alert(alertname="AlertmanagerNotificationsFailing"), T0 + 210)]
    world.firing_at_t0 = [alert(alertname="AlertmanagerNotificationsFailing")]
    assert run(world, tmp_path)[0] == code


# Step 16: the scope.


def test_an_alert_outside_the_changed_set_passes(world, tmp_path):
    world.alerts = [(alert(service="sonarr"), T0 + 60)]
    assert run(world, tmp_path) == (0, "")


@pytest.mark.parametrize(
    "break_base",
    [
        lambda w: setattr(w, "base_verified", False),
        lambda w: setattr(w, "base_revision", R),
        lambda w: setattr(w, "old", "not a digests file"),
    ],
    ids=["unverified", "base-names-r", "malformed"],
)
def test_a_base_that_cannot_be_used_widens_the_scope_to_the_fleet(
    world, tmp_path, break_base
):
    # The red half of the case above: a soak that ignored the scope would pass both or fail both.
    world.alerts = [(alert(service="sonarr"), T0 + 60)]
    break_base(world)
    assert run(world, tmp_path) == (1, "failed-services: sonarr\n")


def test_a_long_rule_on_an_unchanged_service_leaves_w_at_the_changed_value(
    world, tmp_path
):
    assert run(world, tmp_path)[0] == 0
    assert world.now == T0 + 60 + W
    world = World()
    world.new["sonarr"] = {"footprint": "2"}
    assert run(world, tmp_path)[0] == 0
    assert world.now == T0 + 60 + 3600 + 120


def test_an_empty_changed_set_exits_0_at_once(world, tmp_path):
    world.new = world.old
    assert run(world, tmp_path) == (0, "")
    assert world.sleeps == []
    assert not any(path.endswith("/pods") for path in world.reads_of)


# Step 16: the connection and the revision.


def test_a_first_connection_refused_twice_then_answered_passes(tmp_path):
    # A real socket: the port refuses until the second retry's sleep starts a server on it.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    sleeps, servers = [], []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            servers.append(http.server.HTTPServer(("127.0.0.1", port), _Runtimeinfo))
            threading.Thread(target=servers[0].handle_request, daemon=True).start()

    token = tmp_path / "token"
    token.write_text("t")
    reads = flux_soak.live_reads(f"http://127.0.0.1:{port}", str(token), sleep=sleep)
    try:
        assert reads.prometheus_start() == T0
        assert len(sleeps) == 2
    finally:
        for server in servers:
            server.server_close()


class _Runtimeinfo(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"data": {"startTime": iso(T0)}}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


@pytest.mark.parametrize(("named", "code"), [(R, 0), (OTHER, 1)])
def test_the_revision_check_reads_the_origin_key(world, tmp_path, named, code):
    # The red half: `artifact.revision` is `prod@sha256:...`, so a check on it fails both.
    world.homelab_revision = named
    world.new = world.old
    result = run(world, tmp_path)
    assert result[0] == code
    assert (OTHER in result[1]) == bool(code)


def test_the_reserved_suffixes_match_the_renderers():
    assert set(flux_soak.RESERVED_SUFFIXES) == {
        DEFERRED_SUFFIX,
        SNAPSHOT_SUFFIX,
        PREPULL_SUFFIX,
        PRECHECK_SUFFIX,
        VERIFY_SUFFIX,
    }
