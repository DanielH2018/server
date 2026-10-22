"""The artifacts peer sync mails once per outage and pushes a Kuma verdict every run.

`sync-artifacts.sh` runs every 5 minutes on daniel-box and pulls daniel-server's artifact tree.
cron mails whatever the job writes, so a line on every failure would mail on every run. Two
failure shapes matter. One is the peer being unreachable, which is ordinary — daniel-server is
not always on. The other is `bind [127.0.0.1]:8182: Address already in use`, a LocalForward
this host's ssh config attaches to that peer, which `ClearAllForwardings=yes` drops because
rsync's transport needs no forward.

The script is run for real here, with `rsync` and `logger` stubbed as exported bash functions —
bash resolves a function name before it searches PATH. What the runs prove is the streak: every
failure reaches the journal, and only the run that REACHES the threshold writes to stderr, so a
peer that stays down mails once rather than every 5 minutes.

Every run also sources a stub Kuma library that records the verdict instead of pushing it.
Pointing the script at the stub is not optional: daniel-box has the real library, and a sourced
function would override an exported stub.
"""

import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from lib.proc_testing import run as launch
from pathlib import Path

import leakguard
import pytest
from lib import yaml_fast

from _helpers import ROLES, load_defaults
from _shell_render import render_shell_script, rendered_shell_text

CLAUDE_CODE = ROLES / "setup" / "claude_code"
TEMPLATE = ("setup", "claude_code", "sync-artifacts.sh.j2")

DEFAULTS = load_defaults(CLAUDE_CODE)
THRESHOLD = DEFAULTS["claude_code_artifacts_sync_alert_after_failures"]
PEER = DEFAULTS["claude_code_artifacts_peer_sources"][0]["name"]
PEER_SSH = DEFAULTS["claude_code_artifacts_peer_sources"][0]["ssh_target"]
PEER_STARS = DEFAULTS["claude_code_artifacts_peer_sources"][0]["stars_path"]

# Every `rsync` call is recorded. It fails when the stub file says `fail`, and only the star
# push fails when it says `push-fail`, so one rendered script drives every path.
RSYNC_STUB = (
    'printf "%s\\n" "$*" >>"$SYNC_ARTIFACTS_CALLS"; '
    'case "$(cat "$SYNC_ARTIFACTS_STUB")" in fail) return 1 ;; '
    'push-fail) [[ "$*" == *"$SYNC_ARTIFACTS_STARS"* ]] && return 1 ;; esac; return 0'
)
LOGGER_STUB = 'printf "%s\\n" "$*" >>"$SYNC_ARTIFACTS_JOURNAL"'


@dataclass
class _Runner:
    """`run(outcome)` -> CompletedProcess, plus readers for what the run left behind."""

    execute: Callable[[str], subprocess.CompletedProcess]
    journal: Path
    pushes: Path
    calls: Path
    stars: Path

    def __call__(self, outcome: str) -> subprocess.CompletedProcess:
        return self.execute(outcome)

    def verdicts(self) -> list[str]:
        return [line.split("|")[0] for line in self.pushes.read_text().splitlines()]

    def last_push(self) -> str:
        return self.pushes.read_text().splitlines()[-1]

    def rsync_calls(self) -> list[str]:
        return self.calls.read_text().splitlines()


def _runner(tmp_path: Path) -> _Runner:
    """A rendered sync script plus `run(outcome)` -> CompletedProcess."""
    script = tmp_path / "sync-artifacts.sh"
    # The shared accessor renders what the gate lints, with one override: the peer tree has to
    # be a tmp_path, because the run is real. Role defaults come from the accessor's own
    # context rather than being passed in here (#3178).
    stars = tmp_path / "stars.txt"
    script.write_text(
        render_shell_script(
            *TEMPLATE,
            overrides={
                "claude_code_artifacts_peer_dir": str(tmp_path / "peer"),
                "claude_code_artifacts_stars_file": str(stars),
            },
        )
    )
    (tmp_path / "peer" / PEER).mkdir(parents=True)
    stub = tmp_path / "outcome"
    journal = tmp_path / "journal"
    journal.touch()
    pushes = tmp_path / "pushes"
    pushes.touch()
    calls = tmp_path / "rsync-calls"
    calls.touch()
    kuma_lib = tmp_path / "kuma-push-lib.sh"
    kuma_lib.write_text(
        'kuma_push() { printf "%s|%s\\n" "$1" "$2" >>"$SYNC_ARTIFACTS_PUSHES"; }\n'
    )

    def run(outcome: str) -> subprocess.CompletedProcess:
        stub.write_text(outcome)
        return launch(
            ["bash", str(script)],
            env={
                "PATH": os.environ["PATH"],
                "SYNC_ARTIFACTS_STATE_DIR": str(tmp_path),
                "SYNC_ARTIFACTS_STUB": str(stub),
                "SYNC_ARTIFACTS_JOURNAL": str(journal),
                "SYNC_ARTIFACTS_KUMA_LIB": str(kuma_lib),
                "SYNC_ARTIFACTS_PUSHES": str(pushes),
                "SYNC_ARTIFACTS_CALLS": str(calls),
                "SYNC_ARTIFACTS_STARS": str(stars),
                "BASH_FUNC_rsync%%": f"() {{ {RSYNC_STUB}; }}",
                "BASH_FUNC_logger%%": f"() {{ {LOGGER_STUB}; }}",
            },
        )

    return _Runner(run, journal, pushes, calls, stars)


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


def test_the_star_list_is_pushed_to_the_peer_after_the_pull(tmp_path):
    """The peer prunes its own tree, so it keeps a starred artifact only once the list lands."""
    run = _runner(tmp_path)
    run.stars.write_text(f"{PEER}/plan.html\n")
    assert run("ok").returncode == 0
    pull, push = run.rsync_calls()
    assert f"{PEER_SSH}:" in pull and "--delete" in pull, pull
    assert push.endswith(f"{run.stars} {PEER_SSH}:{PEER_STARS}"), push


def test_no_star_list_means_no_push(tmp_path):
    run = _runner(tmp_path)
    assert run("ok").returncode == 0
    assert len(run.rsync_calls()) == 1, run.rsync_calls()


def test_a_failed_star_push_counts_toward_the_streak(tmp_path):
    """Until the push lands, the peer's prune can delete what was starred, so it is not a success."""
    run = _runner(tmp_path)
    run.stars.write_text(f"{PEER}/plan.html\n")
    assert run("push-fail").returncode == 1
    assert f"{PEER} failed (consecutive run 1)" in run.journal.read_text()


def test_the_ssh_transport_drops_inherited_local_forwards():
    """The second error shape's whole fix: a forward this script never asked for.

    `ssh` reads `~/.ssh/config` for every connection, so a `LocalForward` on the peer's host
    entry applies here too — and a bind collision on that port fails the connection, taking the
    sync with it. The flag has no other effect on an rsync transport.
    """
    rendered = rendered_shell_text(*TEMPLATE)
    transports = [line for line in rendered.splitlines() if "-e 'ssh" in line]
    assert transports, rendered
    assert all("ClearAllForwardings=yes" in line for line in transports), transports


def test_the_cron_runs_the_script_this_suite_renders():
    """Non-vacuity: the suite is pointless if the installed cron runs something else."""
    tasks = yaml_fast.safe_load((CLAUDE_CODE / "tasks" / "node_crons.yml").read_text())
    jobs = {
        task["ansible.builtin.cron"]["name"]: task["ansible.builtin.cron"]["job"]
        for task in tasks
        if isinstance(task.get("ansible.builtin.cron"), dict)
    }
    assert jobs["Sync peer Claude artifacts"] == "/usr/local/bin/sync-artifacts.sh", (
        jobs
    )


# The pull handed to the real rsync, locally: `-e` and its transport argument are dropped and
# the `<ssh_target>:` prefix is stripped, so every other flag reaches rsync as rendered.
# leakguard shims `rsync` on PATH, so the stub calls the binary by the path the test resolved
# from the PATH leakguard saved. A copy between two tmp_path trees reaches no host.
LOCAL_RSYNC_STUB = (
    'local a skip= args=(); for a in "$@"; do '
    'if [ -n "$skip" ]; then skip=; continue; fi; '
    'if [ "$a" = -e ]; then skip=1; continue; fi; '
    'args+=("${a#"$SYNC_ARTIFACTS_SSH":}"); done; "$SYNC_ARTIFACTS_REAL_RSYNC" "${args[@]}"'
)


def _peer_directory_task_mode() -> str:
    tasks = yaml_fast.safe_load(
        (ROLES / "setup" / "k8s_host_files" / "tasks" / "artifacts.yml").read_text()
    )
    (task,) = [
        t
        for t in tasks[0]["block"]
        if t["name"] == "Ensure a peer directory exists for every synced host"
    ]
    return task["ansible.builtin.file"]["mode"]


def test_a_sync_leaves_the_peer_directory_at_the_k8s_host_files_mode(tmp_path):
    """k8s_host_files' task and this sync both own the peer directory, so they agree on its mode.

    With the trailing slash, `rsync -a` copies the peer root's own mode onto the destination.
    The peer's 0755 then flipped back and forth against the task's 0750 on every sync and
    every apply (#5524).
    """
    real_rsync = shutil.which("rsync", path=str(leakguard._state["real_path"]))
    if real_rsync is None:
        pytest.skip("rsync is not installed")
    source = tmp_path / "source"
    (source / "sub").mkdir(parents=True)
    (source / "sub" / "plan.html").write_text("<p>plan</p>\n")
    # The peer's own 755, set past the umask. A directory mode, not an exec-bit fixture.
    for directory in (source, source / "sub"):
        os.chmod(directory, 0o755)
    peer_dir = tmp_path / "peer"
    (peer_dir / PEER).mkdir(parents=True)
    (peer_dir / PEER).chmod(0o750)
    script = tmp_path / "sync-artifacts.sh"
    script.write_text(
        render_shell_script(
            *TEMPLATE,
            overrides={
                "claude_code_artifacts_peer_dir": str(peer_dir),
                "claude_code_artifacts_stars_file": str(tmp_path / "stars.txt"),
                "claude_code_artifacts_peer_sources": [
                    {
                        **DEFAULTS["claude_code_artifacts_peer_sources"][0],
                        "path": str(source),
                    }
                ],
            },
        )
    )
    kuma_lib = tmp_path / "kuma-push-lib.sh"
    kuma_lib.write_text("kuma_push() { :; }\n")
    proc = launch(
        ["bash", str(script)],
        env={
            "PATH": os.environ["PATH"],
            "SYNC_ARTIFACTS_STATE_DIR": str(tmp_path),
            "SYNC_ARTIFACTS_KUMA_LIB": str(kuma_lib),
            "SYNC_ARTIFACTS_SSH": PEER_SSH,
            "SYNC_ARTIFACTS_REAL_RSYNC": real_rsync,
            "BASH_FUNC_rsync%%": f"() {{ {LOCAL_RSYNC_STUB}; }}",
            "BASH_FUNC_logger%%": "() { :; }",
        },
    )
    assert proc.returncode == 0, proc
    # Non-vacuity: the pull really ran, so the mode below is the one rsync left.
    assert (peer_dir / PEER / "sub" / "plan.html").read_text() == "<p>plan</p>\n"
    mode = _peer_directory_task_mode()
    assert oct((peer_dir / PEER).stat().st_mode & 0o777) == oct(int(mode, 8)), mode
