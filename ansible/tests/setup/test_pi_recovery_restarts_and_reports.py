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

Two later additions, both tested here. The watch set is every container the host deploys,
not the two the script was born with (#1910): the same failed start reaches glances, wg-easy
and docker-proxy-lifecycle, and only the last is invisible to Kuma from outside. And the
DOWN line records `docker inspect`'s exit code and error BEFORE the restart erases them
(#1912): the Pi's journal rotates in a day, so that line is the only evidence that survives.

Run: uv run pytest ansible/tests/setup/test_pi_recovery_restarts_and_reports.py
"""

import pytest
from _pi_health import PI_HOST_VARS, run


SCRIPT = "pi-recovery-health"

# The watch set the rendered script must carry: every containers_list name on daniel-pi, plus
# the sub-proxy the docker-proxy role's compose file declares beside its list entry. Named
# rather than counted so the failure says which container the cron stopped watching.
PI_CONTAINERS = [c["name"] for c in PI_HOST_VARS["containers_list"]]
EXPECTED_WATCH_SET = {*PI_CONTAINERS, "docker-proxy-lifecycle"}
# The container #1910 was filed on: a compose sub-service, not a containers_list entry, so
# iterating the list alone would miss it.
MUST_WATCH = frozenset(
    {"autoheal", "docker-proxy", "docker-proxy-lifecycle", "glances", "wg-easy"}
)

ALL = sorted(EXPECTED_WATCH_SET)


def test_the_watch_set_is_every_deployed_container(tmp_path):
    """Non-vacuity: the rendered set names each container the Pi deploys, by name."""
    script = tmp_path / f"{SCRIPT}.sh"
    run(SCRIPT, tmp_path, running=ALL)
    body = script.read_text()
    line = next(line for line in body.splitlines() if line.startswith("CONTAINERS=("))
    rendered = set(line[len("CONTAINERS=(") : -1].split())

    assert MUST_WATCH <= rendered, (
        f"missing {sorted(MUST_WATCH - rendered)} from {line!r}"
    )
    assert rendered == EXPECTED_WATCH_SET, (
        f"rendered {sorted(rendered)} but the host deploys {sorted(EXPECTED_WATCH_SET)}"
    )


def test_a_healthy_cycle_pushes_up(tmp_path):
    """The input it must ACCEPT: every container running, so nothing to do."""
    status, msg, _, _ = run(SCRIPT, tmp_path, running=ALL)

    assert status == "up", f"all containers running but pushed {status!r} ({msg})"
    assert msg == f"all {len(ALL)} containers running", msg


def test_a_dead_sub_proxy_is_not_the_all_clear(tmp_path):
    """REJECT: docker-proxy-lifecycle is not a containers_list entry -- it still counts."""
    running = [c for c in ALL if c != "docker-proxy-lifecycle"]
    status, msg, still_running, _ = run(SCRIPT, tmp_path, running=running)

    assert status == "down", (
        f"docker-proxy-lifecycle down but pushed {status!r} ({msg})"
    )
    assert "docker-proxy-lifecycle" in still_running


@pytest.mark.parametrize("dead", ALL)
def test_a_dead_container_is_restarted(tmp_path, dead):
    """The restart half: the script brings it back rather than only naming it."""
    running = [c for c in ALL if c != dead]
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
        SCRIPT,
        tmp_path,
        running=[c for c in ALL if c != "autoheal"],
        unstartable="autoheal",
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
        SCRIPT,
        tmp_path,
        running=[c for c in ALL if c != "autoheal"],
        unstartable=unstartable,
    )

    assert "containers running" not in msg, (
        f"reported the all-clear with autoheal down (unstartable={unstartable!r})"
    )
    assert status == "down"


OCI_ERROR = "unable to start unit: Timeout waiting for systemd to create scope"


def test_the_down_line_names_the_exit_reason_before_the_restart(tmp_path):
    """ACCEPT (#1912): the message carries inspect's state from BEFORE `docker start`.

    The stub answers status=running once a container is started, so an `exit=137` in the
    message proves the inspect ran first -- after the restart, the reason is gone for good.
    """
    running = [c for c in ALL if c != "autoheal"]
    status, msg, still_running, lines = run(
        SCRIPT, tmp_path, running=running, exit_code="137", error=OCI_ERROR
    )

    assert status == "down"
    assert "autoheal" in still_running, "restart still has to happen after the inspect"
    assert "status=exited exit=137" in msg, f"no exit reason in {msg!r}"
    assert OCI_ERROR in msg, f"daemon error missing from {msg!r}"
    assert "status=running" not in msg, (
        "inspected after the restart -- the reason is gone"
    )
    assert lines and OCI_ERROR in lines[0], (
        f"health.log line lacks the reason: {lines!r}"
    )


def test_a_multi_line_daemon_error_stays_one_health_log_record(tmp_path):
    """REJECT: a newline in State.Error must not split the one line Loki reconstructs."""
    running = [c for c in ALL if c != "autoheal"]
    _, msg, _, lines = run(
        SCRIPT, tmp_path, running=running, error="first line\nsecond line"
    )

    assert len(lines) == 1, f"the daemon error split the record: {lines!r}"
    assert "first line second line" in msg, msg
