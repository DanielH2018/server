#!/usr/bin/env python3
"""pi-recovery-health.sh restarts a dead container AND still reports the cycle as DOWN.

The script used to only detect. On 2026-08-29 autoheal died with an OCI create failure
("Timeout waiting for systemd to create scope") under memory pressure on daniel-pi, and
`restart: unless-stopped` never retried it -- that policy covers a container whose process
exits, not one whose create fails -- so it stayed down ~50 minutes until a human ran
`docker start`. Detection worked; remediation did not exist.

Adding a restart introduces the opposite hazard, which is why the reporting half is tested
just as hard as the restart half: a self-healing cron that pushes `up` after a successful
restart makes a container crashing every 5 minutes read green forever. So a cycle that had
to intervene pushes DOWN, and only a clean cycle pushes `up`.

Run: uv run pytest ansible/tests/setup/test_pi_recovery_restarts_and_reports.py
"""

import pytest
from _pi_health import run


BOTH = ["autoheal", "docker-proxy"]
SCRIPT = "pi-recovery-health"


def test_a_healthy_cycle_pushes_up(tmp_path):
    """The input it must ACCEPT: both containers running, so nothing to do."""
    status, msg, _, _ = run(SCRIPT, tmp_path, running=BOTH)

    assert status == "up", f"both containers running but pushed {status!r} ({msg})"


@pytest.mark.parametrize("dead", BOTH)
def test_a_dead_container_is_restarted(tmp_path, dead):
    """The restart half: the script brings it back rather than only naming it."""
    running = [c for c in BOTH if c != dead]
    status, msg, still_running, _ = run(SCRIPT, tmp_path, running=running)

    assert dead in still_running, (
        f"{dead} was down and the script left it down -- `restart: unless-stopped` does not "
        "cover an OCI create failure, so nothing else will start it"
    )
    assert f"restarted: {dead}" in msg, f"restart not reported in {msg!r}"
    assert status == "down", (
        f"pushed {status!r} after recovering {dead}. A cycle that had to intervene must push "
        "down, or a container crashlooping every 5 minutes reads green forever."
    )


def test_a_restart_that_fails_is_reported_as_failed(tmp_path):
    """The input it must REJECT: down AND unrecoverable -- the 2026-08-29 state itself."""
    status, msg, still_running, _ = run(
        SCRIPT, tmp_path, running=["docker-proxy"], unstartable="autoheal"
    )

    assert "autoheal" not in still_running
    assert status == "down"
    assert "restart FAILED: autoheal" in msg, (
        f"a failed restart must say so -- got {msg!r}. Without it an operator cannot tell a "
        "self-healed blip from a container that is never coming back."
    )


@pytest.mark.parametrize("unstartable", ["", "autoheal"])
def test_the_all_clear_is_unreachable_while_a_container_is_down(tmp_path, unstartable):
    """Guards the branch itself: no restart outcome may report the all-clear."""
    status, msg, _, _ = run(
        SCRIPT, tmp_path, running=["docker-proxy"], unstartable=unstartable
    )

    assert "autoheal + docker-proxy running" not in msg, (
        f"reported the all-clear with autoheal down (unstartable={unstartable!r})"
    )
    assert status == "down"
