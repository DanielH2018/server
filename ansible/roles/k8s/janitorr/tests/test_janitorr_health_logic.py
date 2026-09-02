import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "files")
)
from janitorr_health_logic import (
    effective_window_s,
    janitorr_errors_ok,
)

HOUR = 3600.0
WINDOW = 12 * HOUR
GRACE = 600.0


def test_no_running_pod_defers_rather_than_paging():
    # k8s Workload Health owns "janitorr is not running" via
    # kube_deployment_status_replicas_unavailable. Paging here too would be one outage, two alerts.
    ok, msg = janitorr_errors_ok(None, None, WINDOW, GRACE)
    assert ok
    assert "no running janitorr pod" in msg


def test_inside_startup_grace_is_ok_even_with_errors():
    # The documented boot race: an @Scheduled cleanup fires before the *arrs are up and logs a
    # generic ERROR indistinguishable from a real failure.
    ok, msg = janitorr_errors_ok(3, 120, WINDOW, GRACE)
    assert ok
    assert "startup grace" in msg


def test_errors_past_grace_page():
    ok, msg = janitorr_errors_ok(2, 4 * HOUR, WINDOW, GRACE)
    assert not ok
    assert "2 janitorr scheduled-task error(s)" in msg


def test_clean_past_grace_is_ok_and_reports_uptime():
    ok, msg = janitorr_errors_ok(0, 4 * HOUR, WINDOW, GRACE)
    assert ok
    assert "up 4.0h" in msg


def test_none_count_is_treated_as_zero():
    ok, _ = janitorr_errors_ok(None, 4 * HOUR, WINDOW, GRACE)
    assert ok


def test_message_points_at_kubectl_not_docker():
    # The Docker-era message said `docker logs janitorr`, which is the wrong host now.
    _, msg = janitorr_errors_ok(1, 4 * HOUR, WINDOW, GRACE)
    assert "kubectl" in msg and "docker" not in msg


def test_window_is_capped_to_the_post_startup_slice():
    # A janitorr that restarted 20 minutes ago must not have its own boot-race errors counted for
    # the next 12 hours: the slice ends where grace ended.
    assert effective_window_s(20 * 60, WINDOW, GRACE) == 20 * 60 - GRACE


def test_window_is_the_full_window_once_uptime_is_long():
    assert effective_window_s(40 * HOUR, WINDOW, GRACE) == WINDOW
