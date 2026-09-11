#!/usr/bin/env python3
"""What monitor-bridge WRITES is what probe_lib/alerts.py can READ.

The join is a FORMAT, and each side is edited by someone who cannot see the other. `check.py`
logs `bridge.common.log("DOWN", name, "-", msg)`; `probe.py alerts` reconstructs episodes by
regex over those lines. When bec8990a dropped the bracketed stamp `log()` used to print — the
container's Chicago wall clock wearing an ISO face, beside the runtime's own true stamp — the
reader still required it, and every monitor-bridge episode stopped being reconstructed. That
failure prints "no DOWN alerts", so it reads as a healthy fleet rather than as an error (#1782).

The sibling guard for the other stream is ansible/tests/setup/test_pi_health_log_line_shape.py.
Nothing here matches on source text: it runs the real `log()` and feeds its output to the real
parser.

Run: uv run pytest ansible/tests/services/test_monitor_bridge_down_line_shape.py
"""

import bridge.common

from diagnostics.probe_lib.alerts import parse_down_line


def _logged(capsys, *args):
    bridge.common.log(*args)
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 1, f"expected exactly one log line, got {out!r}"
    return out[0]


def test_a_down_line_the_bridge_logs_parses_into_an_episode(capsys):
    """ACCEPT: the emitted DOWN line is what the alert reconstruction consumes."""
    line = _logged(
        capsys,
        "DOWN",
        "traefik_latency",
        "-",
        "1 service(s) with over 5% of requests slower than 5.0s",
    )

    assert parse_down_line(line) == (
        "traefik_latency",
        "1 service(s) with over 5% of requests slower than 5.0s",
    )


def test_an_ok_line_the_bridge_logs_is_not_an_episode(capsys):
    """REJECT: the same emitter's healthy line must not become a DOWN episode."""
    line = _logged(capsys, "OK  ", "traefik_latency", "-", "no slow services")

    assert parse_down_line(line) is None
