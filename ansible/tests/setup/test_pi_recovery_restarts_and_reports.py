#!/usr/bin/env python3
"""pi-recovery-health restarts a dead container AND still reports the cycle as DOWN.

A container whose create fails (autoheal died with an OCI create failure, "Timeout waiting
for systemd to create scope", under memory pressure) is never retried by
`restart: unless-stopped` -- that policy covers a container whose process exits, not one
whose create fails -- so it stays down until a human runs `docker start`. The script
therefore remediates as well as detects.

Adding a restart introduces the opposite hazard, which is why the reporting half is tested
just as hard as the restart half: a self-healing heartbeat that pushes `up` after a successful
restart makes a container crashing every 5 minutes read green forever. So a cycle that had
to intervene pushes DOWN, and only a clean cycle pushes `up`.

Three further properties are tested here. The watch set is every container the host
deploys: the same failed start reaches glances, wg-easy and docker-proxy-lifecycle, and only
the last is invisible to Kuma from outside. The DOWN line records `docker inspect`'s exit
code and error BEFORE the restart erases them: the Pi's journal rotates in a day, so that
line is the only evidence that survives. And one case covers the daemon rather than a
container: when dockerd itself is gone every container reads `inspect failed` and the reason
lives only in `journalctl -u docker`, so the line carries the newest non-info lines of that
unit — through a stub, since the real journalctl on PATH would read this host's journal.

One property here is not about the script at all: the timer that runs it must be disarmable
from the inventory. homelab-daniel-pi ships the timer's enable link only while
`optimize_pi_recovery_restart_enabled` is true, and reconcile stops a timer whose link the new
version drops (H3.8b). The container case in `scripts/validate/host_package_cases.py` proves
the stop on a real systemd; the render tests here prove the link follows the flag.

Run: uv run pytest ansible/tests/setup/test_pi_recovery_restarts_and_reports.py
"""

import os

import pytest
from _pi_health import COMPONENT_DEFAULTS, PI_HOST_VARS, journalctl_calls, run
from lib.ansible_inventory import pi_entries_in
from renderer.renderer_lib import host as host_mode
from validate.validate_lib.host_fixtures import (
    HOST,
    RECOVERY_FLAG,
    RECOVERY_TIMER,
    disarmed_inventory,
)

SCRIPT = "pi-recovery-health"
UNIT_DIR = "/usr/lib/systemd/system"
LINK = f"{UNIT_DIR}/timers.target.wants/{RECOVERY_TIMER}"

# The watch set the rendered script must carry: every containers_list and compose_services name
# on daniel-pi, plus the sub-proxy the docker-proxy service file declares beside its entry. Named rather than counted so the failure says which container the script stopped
# watching.
PI_CONTAINERS = [entry["name"] for entry in pi_entries_in(PI_HOST_VARS)]
EXPECTED_WATCH_SET = {*PI_CONTAINERS, "docker-proxy-lifecycle"}
# A compose sub-service, not a containers_list entry, so iterating the list alone would miss it.
MUST_WATCH = frozenset(
    {"autoheal", "docker-proxy", "docker-proxy-lifecycle", "wg-easy"}
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


def _watch_set(tmp_path, jinja_vars) -> set[str]:
    run(SCRIPT, tmp_path, running=ALL, jinja_vars=jinja_vars)
    body = (tmp_path / f"{SCRIPT}.sh").read_text()
    line = next(line for line in body.splitlines() if line.startswith("CONTAINERS=("))
    return set(line[len("CONTAINERS=(") : -1].split())


def test_a_container_moved_to_compose_services_is_still_watched(tmp_path):
    """ACCEPT and REJECT: autoheal in the second key stays watched (H5.1).

    The fixture is what makes a derivation over `containers_list` alone fail: that derivation
    would name every container but autoheal here, the REJECT half of this case.
    """
    kept = [c for c in PI_HOST_VARS["containers_list"] if c["name"] != "autoheal"]
    moved = [c for c in PI_HOST_VARS["containers_list"] if c["name"] == "autoheal"]
    jinja_vars = {
        "containers_list": kept,
        "compose_services": [*PI_HOST_VARS["compose_services"], *moved],
    }

    assert "autoheal" not in {c["name"] for c in kept}
    assert _watch_set(tmp_path, jinja_vars) == EXPECTED_WATCH_SET


def test_the_watch_set_is_independent_of_which_key_holds_an_entry(tmp_path):
    """The rendered script is byte-identical across a move, so the package does not churn."""
    whole = tmp_path / "whole"
    split = tmp_path / "split"
    whole.mkdir()
    split.mkdir()
    pi = PI_HOST_VARS["containers_list"]
    run(SCRIPT, whole, running=ALL)
    run(
        SCRIPT,
        split,
        running=ALL,
        jinja_vars={
            "containers_list": pi[:2],
            "compose_services": [*pi[2:], *PI_HOST_VARS["compose_services"]],
        },
    )
    # The harness repoints two absolute paths into the run's own directory; the rest is the render.
    rendered = [
        (d / f"{SCRIPT}.sh").read_text().replace(str(d), "<run>")
        for d in (whole, split)
    ]
    assert rendered[0] == rendered[1]


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
    """The input it must REJECT: down AND unrecoverable -- the autoheal create-failure state."""
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
    """ACCEPT: the message carries inspect's state from BEFORE `docker start`.

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
    """ACCEPT: every container reads `inspect failed`, so the line carries dockerd's
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
    running = [c for c in ALL if c != "wg-easy"]
    status, msg, _, _ = run(
        SCRIPT, tmp_path, running=running, gone="wg-easy", journal=DAEMON_JOURNAL
    )

    assert status == "down"
    assert "wg-easy [inspect failed]" in msg, msg
    assert "restart FAILED: wg-easy" in msg, msg
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


def _compose_prom(tmp_path, body):
    """Seed homelab_compose.prom in the textfile directory the harness gives the script."""
    textfiles = tmp_path / "textfile"
    textfiles.mkdir(exist_ok=True)
    (textfiles / "homelab_compose.prom").write_text(body)
    return textfiles / "homelab_compose.prom"


def _result(value, sha="0123456789ab"):
    return f'homelab_compose_up_result{{host="daniel-pi",sha="{sha}"}} {value}\n'


def test_a_compose_result_of_zero_pushes_down_naming_the_unit_and_the_sha(tmp_path):
    """REJECT (H5.2b): every container runs, yet the package's last run failed.

    The container loop alone reads all-clear here, because a failed run leaves containers as
    they were. A script without the compose arm pushes `up` and fails this case.
    """
    _compose_prom(tmp_path, _result(0))
    status, msg, _, lines = run(SCRIPT, tmp_path, running=ALL)

    assert status == "down"
    assert "homelab-compose" in msg and "0123456789ab" in msg, msg
    assert "not running" not in msg, msg
    assert lines and "homelab-compose" in lines[0], lines


def test_a_compose_result_of_one_adds_nothing(tmp_path):
    """ACCEPT: 1 is success. No file is the other ACCEPT, which the healthy-cycle case covers."""
    _compose_prom(tmp_path, _result(1))
    status, msg, _, _ = run(SCRIPT, tmp_path, running=ALL)

    assert (status, msg) == ("up", f"all {len(ALL)} containers running")


def test_a_compose_failure_and_a_dead_container_are_both_reported(tmp_path):
    _compose_prom(tmp_path, _result(0))
    status, msg, _, _ = run(
        SCRIPT, tmp_path, running=[c for c in ALL if c != "autoheal"]
    )

    assert status == "down"
    assert "restarted: autoheal" in msg and "homelab-compose" in msg, msg


def _unreadable_by_mode(path):
    path.chmod(0)


def _unreadable_by_kind(path):
    path.unlink()
    path.mkdir()


@pytest.mark.parametrize(
    "unreadable",
    [
        pytest.param(
            _unreadable_by_mode,
            id="mode-0",
            marks=pytest.mark.skipif(
                os.geteuid() == 0,
                reason="root reads a mode-0 file; the directory case covers root",
            ),
        ),
        pytest.param(_unreadable_by_kind, id="directory"),
    ],
)
def test_a_compose_file_that_cannot_be_read_is_down_never_absent(tmp_path, unreadable):
    """REJECT: treating an unreadable file as absent would turn a broken signal into all-clear."""
    unreadable(_compose_prom(tmp_path, _result(1)))
    status, msg, _, _ = run(SCRIPT, tmp_path, running=ALL)

    assert status == "down"
    assert "homelab_compose.prom exists and cannot be read" in msg, msg


def test_a_compose_file_with_no_result_line_is_down(tmp_path):
    _compose_prom(tmp_path, "# a truncated write\n")
    status, msg, _, _ = run(SCRIPT, tmp_path, running=ALL)

    assert status == "down"
    assert "holds no homelab_compose_up_result" in msg, msg


def _renders(tmp_path) -> tuple[host_mode.Package, host_mode.Package]:
    """The real Pi package rendered armed, from the repo's inventory, and disarmed."""
    armed = host_mode.render_host(HOST)
    disarmed = host_mode.render_host(HOST, inventory=disarmed_inventory(tmp_path))
    return armed, disarmed


def test_the_timer_can_be_disarmed_from_the_inventory(tmp_path):
    """A one-way arming switch is a bug: adding a way in means adding the way out."""
    armed, disarmed = _renders(tmp_path)
    assert LINK in armed.links, f"the armed render ships no {LINK}"
    assert LINK not in disarmed.links, (
        f"{RECOVERY_FLAG}: false still ships {LINK}, so disarming the timer means editing "
        "the component rather than flipping a host_vars flag"
    )


def test_the_timer_ships_armed():
    """The live Pi runs this heartbeat; a default-off flag would have the next install stop it."""
    assert COMPONENT_DEFAULTS[RECOVERY_FLAG] is True, (
        "the heartbeat has run unconditionally since #1910 and is the only thing that "
        "restarts a container whose create failed at the OCI runtime"
    )


def test_disarming_leaves_the_script_installed(tmp_path):
    """Disarming stops the schedule, not the ability to run one cycle by hand."""
    _, disarmed = _renders(tmp_path)
    for path in (
        "/usr/lib/homelab/pi-recovery-health",
        f"{UNIT_DIR}/homelab-pi-recovery.service",
        f"{UNIT_DIR}/{RECOVERY_TIMER}",
    ):
        assert path in disarmed.files, (
            f"the disarmed render drops {path}: an operator disarming the timer still needs "
            "to be able to start one cycle by hand"
        )
