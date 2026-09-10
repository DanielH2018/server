"""The Loki read-route witness must go DOWN on the body that broke it, not just on a bad exit code.

`probe.py` runs curl without `-f`, so the 404 Traefik answers when the route's ClientIP guard
refuses a host (issue #1693) arrives as exit 0 with the body `404 page not found`. The accept
half below is the live response measured from daniel-server on 2026-09-10; the reject halves are
that 404 and the two shapes a route can answer while carrying nothing.

Run: uv run pytest ansible/roles/setup/initial_setup/tests/test_loki_route_health.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "files"))

from loki_route_health import verdict

# Measured from daniel-server, `uv run python scripts/diagnostics/probe.py loki-labels`.
LIVE = (
    '{"status":"success","data":["container","filename","job","machine","namespace","pod",'
    '"service_name","stream"]}'
)


def test_a_live_labels_response_is_up():
    status, msg = verdict(0, LIVE)
    assert status == "up", msg
    assert "8 label names" in msg


def test_the_404_body_probe_py_reports_with_exit_zero_is_down():
    """The exact case #1712 exists for — exit 0, and no labels anywhere in the answer."""
    status, msg = verdict(0, "404 page not found\n")
    assert status == "down"
    assert "404 page not found" in msg


def test_an_empty_label_set_is_down():
    status, _ = verdict(0, '{"status":"success","data":[]}')
    assert status == "down"


def test_a_non_success_status_is_down():
    status, _ = verdict(0, '{"status":"error","error":"too many outstanding requests"}')
    assert status == "down"


def test_a_failed_probe_run_is_down():
    status, msg = verdict(2, "Traceback (most recent call last):")
    assert status == "down"
    assert "exited 2" in msg


def test_the_message_is_trimmed_so_a_whole_error_page_cannot_ride_into_the_push():
    _, msg = verdict(0, "x" * 5000)
    assert len(msg) < 250
