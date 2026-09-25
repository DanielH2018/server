"""The artifacts peer sync mails once per outage (#2467) and pushes a Kuma verdict every run (#2516).

`sync-artifacts.sh` runs every 5 minutes on daniel-box and pulls daniel-server's artifact tree.
cron mails whatever the job writes, and the old script wrote a line on every failure: 145
messages reached /var/mail/ubuntu between 2026-08-20 and 2026-09-24, in two shapes. One was the
peer being unreachable, which is ordinary — daniel-server is not always on. The other was
`bind [127.0.0.1]:8182: Address already in use`, a LocalForward this host's ssh config attaches
to that peer, which `ClearAllForwardings=yes` now drops because rsync's transport needs no
forward.

The script is run for real here, with `rsync` and `logger` stubbed as exported bash functions —
bash resolves a function name before it searches PATH. What the runs prove is the streak: every
failure reaches the journal, and only the run that REACHES the threshold writes to stderr, so a
peer that stays down mails once rather than every 5 minutes.

Every run also sources a stub Kuma library that records the verdict instead of pushing it.
Pointing the script at the stub is not optional: daniel-box has the real library, and a sourced
function would override an exported stub.
"""

import os
import subprocess
from pathlib import Path

from jinja2 import Environment
from lib import yaml_fast

from _helpers import ROLES, load_defaults

ARTIFACTS = ROLES / "k8s" / "artifacts"
SCRIPT = ARTIFACTS / "templates" / "sync-artifacts.sh.j2"

DEFAULTS = load_defaults(ARTIFACTS)
THRESHOLD = DEFAULTS["artifacts_sync_alert_after_failures"]
PEER = DEFAULTS["artifacts_peer_sources"][0]["name"]

# `rsync` fails when the stub file says `fail`, so one rendered script drives both paths.
RSYNC_STUB = '[[ "$(cat "$SYNC_ARTIFACTS_STUB")" == fail ]] && return 1; return 0'
LOGGER_STUB = 'printf "%s\\n" "$*" >>"$SYNC_ARTIFACTS_JOURNAL"'


def _runner(tmp_path: Path):
    """A rendered sync script plus `run(outcome)` -> CompletedProcess."""
    defaults = dict(DEFAULTS)
    defaults["artifacts_peer_dir"] = str(tmp_path / "peer")
    script = tmp_path / "sync-artifacts.sh"
    script.write_text(Environment().from_string(SCRIPT.read_text()).render(**defaults))
    (tmp_path / "peer" / PEER).mkdir(parents=True)
    stub = tmp_path / "outcome"
    journal = tmp_path / "journal"
    journal.touch()
    pushes = tmp_path / "pushes"
    pushes.touch()
    kuma_lib = tmp_path / "kuma-push-lib.sh"
    kuma_lib.write_text(
        'kuma_push() { printf "%s|%s\\n" "$1" "$2" >>"$SYNC_ARTIFACTS_PUSHES"; }\n'
    )

    def run(outcome: str) -> subprocess.CompletedProcess:
        stub.write_text(outcome)
        return subprocess.run(
            ["bash", str(script)],
            env={
                "PATH": os.environ["PATH"],
                "SYNC_ARTIFACTS_STATE_DIR": str(tmp_path),
                "SYNC_ARTIFACTS_STUB": str(stub),
                "SYNC_ARTIFACTS_JOURNAL": str(journal),
                "SYNC_ARTIFACTS_KUMA_LIB": str(kuma_lib),
                "SYNC_ARTIFACTS_PUSHES": str(pushes),
                "BASH_FUNC_rsync%%": f"() {{ {RSYNC_STUB}; }}",
                "BASH_FUNC_logger%%": f"() {{ {LOGGER_STUB}; }}",
            },
            capture_output=True,
            text=True,
        )

    run.journal = journal
    run.verdicts = lambda: [
        line.split("|")[0] for line in pushes.read_text().splitlines()
    ]
    run.last_push = lambda: pushes.read_text().splitlines()[-1]
    return run


def test_a_sustained_outage_mails_on_the_threshold_run_alone(tmp_path):
    run = _runner(tmp_path)
    runs = [run("fail") for _ in range(THRESHOLD + 3)]
    mailed = [bool(proc.stderr) for proc in runs]
    assert mailed.count(True) == 1, mailed
    assert mailed.index(True) == THRESHOLD - 1, mailed
    stderr = runs[THRESHOLD - 1].stderr
    assert PEER in stderr and str(THRESHOLD) in stderr, stderr
    # Every run still reached the journal, which is where an outage is actually readable.
    assert run.journal.read_text().count("failed (consecutive run") == THRESHOLD + 3


def test_a_recovery_resets_the_streak(tmp_path):
    """Without the reset, one long outage would leave the counter past the threshold forever.

    A later outage would then never mail — the counter would step straight past the `-eq` test
    that arms the alert.
    """
    run = _runner(tmp_path)
    for _ in range(THRESHOLD):
        run("fail")
    assert not run("ok").stderr
    assert "recovered after %d failed run(s)" % THRESHOLD in run.journal.read_text()

    mailed = [bool(run("fail").stderr) for _ in range(THRESHOLD)]
    assert mailed.count(True) == 1, mailed
    assert mailed.index(True) == THRESHOLD - 1, mailed


def test_a_successful_run_is_silent_on_both_streams(tmp_path):
    run = _runner(tmp_path)
    proc = run("ok")
    assert proc.returncode == 0
    assert not proc.stdout and not proc.stderr, proc
    assert run.journal.read_text() == ""


def test_a_healthy_run_pushes_up(tmp_path):
    run = _runner(tmp_path)
    run("ok")
    assert run.last_push() == f"up|{PEER} synced"


def test_the_tile_stays_up_below_the_threshold_and_names_the_streak(tmp_path):
    run = _runner(tmp_path)
    for _ in range(THRESHOLD - 1):
        run("fail")
    assert run.verdicts() == ["up"] * (THRESHOLD - 1)
    assert f"{PEER} failed {THRESHOLD - 1} consecutive run(s)" in run.last_push()


def test_the_tile_goes_down_at_the_threshold_and_stays_down_past_it(tmp_path):
    """Past the threshold matters: the mail's `-eq` test would turn the tile green again."""
    run = _runner(tmp_path)
    for _ in range(THRESHOLD + 3):
        run("fail")
    assert run.verdicts() == ["up"] * (THRESHOLD - 1) + ["down"] * 4
    assert run.last_push() == f"down|{PEER} failed {THRESHOLD + 3} consecutive runs"


def test_a_recovery_pushes_up_again(tmp_path):
    run = _runner(tmp_path)
    for _ in range(THRESHOLD):
        run("fail")
    run("ok")
    assert run.verdicts()[-2:] == ["down", "up"]


def test_the_ssh_transport_drops_inherited_local_forwards():
    """The second error shape's whole fix: a forward this script never asked for.

    `ssh` reads `~/.ssh/config` for every connection, so a `LocalForward` on the peer's host
    entry applies here too — and a bind collision on that port fails the connection, taking the
    sync with it. The flag has no other effect on an rsync transport.
    """
    rendered = Environment().from_string(SCRIPT.read_text()).render(**DEFAULTS)
    transports = [line for line in rendered.splitlines() if "-e 'ssh" in line]
    assert transports, rendered
    assert all("ClearAllForwardings=yes" in line for line in transports), transports


def test_the_cron_runs_the_script_this_suite_renders():
    """Non-vacuity: the suite is pointless if the installed cron runs something else."""
    tasks = yaml_fast.safe_load((ARTIFACTS / "tasks" / "main.yml").read_text())
    jobs = [
        task["ansible.builtin.cron"]["job"]
        for task in tasks
        if isinstance(task.get("ansible.builtin.cron"), dict)
    ]
    assert jobs == ["/usr/local/bin/sync-artifacts.sh"], jobs
