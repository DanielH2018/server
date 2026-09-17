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
A third covers the daemon rather than a container (#1922): when dockerd itself is gone every
container reads `inspect failed` and the reason lives only in `journalctl -u docker`, so the
line carries the newest non-info lines of that unit — through a stub, since the real
journalctl on PATH would read this host's journal.

Run: uv run pytest ansible/tests/setup/test_pi_recovery_restarts_and_reports.py
"""

import pytest
from _pi_health import PI_HOST_VARS, journalctl_calls, run


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


# What dockerd's journal holds when the daemon itself fails: systemd's unit lines carry no
# `level=`, dockerd's own carry one, and the info chatter around them is what must NOT be kept.
DAEMON_JOURNAL = (
    'time="2026-09-13T07:36:30Z" level=info msg="Loading containers: start."\\n'
    'time="2026-09-13T07:36:31Z" level=warning msg="Security options with `:` as a separator are deprecated"\\n'
    'time="2026-09-13T07:36:32Z" level=error msg="failed to start container" error="Timeout waiting for systemd to create scope"\\n'
    "docker.service: Main process exited, code=killed, status=9/KILL\\n"
    'time="2026-09-13T07:36:33Z" level=info msg="Daemon shutdown complete"\\n'
    "docker.service: Scheduled restart job, restart counter is at 3.\\n"
)


def test_a_dead_daemon_is_named_from_its_own_journal(tmp_path):
    """ACCEPT (#1922): every container reads `inspect failed`, so the line carries dockerd's
    journal — the only copy that outlives the Pi's 32M journal rotation is this record.
    """
    status, msg, _, lines = run(
        SCRIPT, tmp_path, running=ALL, daemon_down=True, journal=DAEMON_JOURNAL
    )

    assert status == "down"
    assert msg.count("inspect failed") == len(ALL), msg
    assert "; dockerd: " in msg, f"no daemon evidence in {msg!r}"
    daemon = msg.split("; dockerd: ", 1)[1]
    assert "Timeout waiting for systemd to create scope" in daemon, daemon
    assert "Main process exited, code=killed, status=9/KILL" in daemon, daemon
    assert "Scheduled restart job" in daemon, daemon
    assert len(lines) == 1 and "Scheduled restart job" in lines[0], lines
    # The record's own timestamp already says when; dockerd's is dropped, not the message.
    assert 'time="' not in daemon, daemon
    calls = journalctl_calls(tmp_path)
    assert calls and all("-u docker" in c for c in calls), calls


def test_daemon_info_chatter_and_the_create_deprecation_are_not_kept(tmp_path):
    """REJECT: `level=info` lines and the per-create `Security options` warning are noise that
    would crowd the five lines kept — dockerd emits the warning on every container create.
    """
    _, msg, _, _ = run(
        SCRIPT, tmp_path, running=ALL, daemon_down=True, journal=DAEMON_JOURNAL
    )
    daemon = msg.split("; dockerd: ", 1)[1]

    assert "Loading containers" not in daemon, daemon
    assert "Daemon shutdown complete" not in daemon, daemon
    assert "Security options with" not in daemon, daemon


def test_a_container_death_does_not_read_the_daemon_journal(tmp_path):
    """REJECT: `inspect` answered, so the reason is the container's own and the journal is
    not consulted — a `dockerd:` section here would blame the daemon for a container exit.
    """
    running = [c for c in ALL if c != "autoheal"]
    _, msg, _, _ = run(
        SCRIPT, tmp_path, running=running, journal=DAEMON_JOURNAL, error=OCI_ERROR
    )

    assert "dockerd:" not in msg, msg
    assert journalctl_calls(tmp_path) == [], "journal read for a container-level death"


def test_a_daemon_that_returned_during_the_cycle_is_still_named(tmp_path):
    """ACCEPT: a restart loop can bring dockerd back between the loop and the direct probe.
    Every container reading `inspect failed` is the fingerprint the second arm keys on.
    """
    _, msg, _, _ = run(
        SCRIPT,
        tmp_path,
        running=ALL,
        daemon_down=True,
        daemon_back=True,
        journal=DAEMON_JOURNAL,
    )

    assert msg.count("inspect failed") == len(ALL), msg
    assert "Scheduled restart job" in msg, f"daemon evidence lost: {msg!r}"


def test_one_container_mid_recreate_does_not_blame_the_daemon(tmp_path):
    """REJECT: a deploy's recreate window reads `inspect failed` for ONE container while the
    daemon answers for the rest — that is not a daemon failure, and the journal is not read.
    """
    running = [c for c in ALL if c != "glances"]
    status, msg, _, _ = run(
        SCRIPT, tmp_path, running=running, gone="glances", journal=DAEMON_JOURNAL
    )

    assert status == "down"
    assert "glances [inspect failed]" in msg, msg
    assert "restart FAILED: glances" in msg, msg
    assert "dockerd:" not in msg, msg
    assert journalctl_calls(tmp_path) == [], "journal read for one missing container"


def test_a_dead_daemon_with_an_empty_journal_still_reports(tmp_path):
    """An empty ten-minute window adds nothing and breaks nothing: the seven `inspect failed`
    entries stand on their own.
    """
    status, msg, _, lines = run(
        SCRIPT, tmp_path, running=ALL, daemon_down=True, journal=""
    )

    assert status == "down"
    assert "dockerd:" not in msg, msg
    assert len(lines) == 1, lines
