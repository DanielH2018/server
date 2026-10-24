"""Guards on the weekly rkhunter scan's Kuma verdict (#4598).

The cron used to pipe `rkhunter --check` into `logger`, so the pipeline's status was logger's and
a scan that failed outright read the same as one that passed. rkhunter-check.sh pushes one Kuma
tile per host and holds four properties:

A SCAN THAT DID NOT COMPLETE PUSHES DOWN. rkhunter exits 1 on a warning and on an error alike, so
the script reads completion from the log this run wrote, and a log left from last week reads as
no scan at all.

ANY WARNING, SUSPECT FILE OR POSSIBLE ROOTKIT PUSHES DOWN (#4638). The warnings every host raised
every week are allowlisted in rkhunter.conf.local, so a healthy week has none; the script's
`DECIDED:` marker has the evidence.

ONE TOKEN PER HOST. A shared token would let one host's `up` cover another whose scan stopped.

THE DEADLINE COVERS A WEEK. The producer is a weekly cron, so a shorter deadline fires DOWN
with nothing wrong, and one past two weeks lets a second missed run go unreported.

Run: uv run pytest ansible/tests/setup/test_rkhunter_check.py
"""

import os

import jinja2

from lib.proc_testing import fake_bin, run, write_exec
from _helpers import ROLES, load_tasks, walk_tasks
from _kuma_monitors import entity, tile_is_gated_on
from _setup_render import render_setup_text
from _shell_render import render_shell_script
from test_aide_check import LOGGER_STUB, _textfile

SCRIPT = ("setup", "initial_setup", "rkhunter-check.sh.j2")
PUSH_ENV = "rkhunter-kuma-push.env.j2"
CONF_LOCAL = "rkhunter.conf.local.j2"
INTEGRITY = ROLES / "setup" / "initial_setup" / "tasks" / "integrity.yml"
CRON_NAME = "Weekly rkhunter malware scan"

# Each host's tile and the variable it is gated on.
TILES = {
    "daniel-box": ("rkhunter-daniel-box.json", "rkhunter_daniel_box_push_token"),
    "daniel-server": (
        "rkhunter-daniel-server.json",
        "rkhunter_daniel_server_push_token",
    ),
    "daniel-pi": ("rkhunter-daniel-pi.json", "rkhunter_daniel_pi_push_token"),
}
WEEK_S = 7 * 24 * 3600
# 2026-09-30 02:02:01 UTC, the run the stale log below records.
LAST_WEEK = 1790733721

REAL_LIB = "/usr/local/lib/kuma-push-lib.sh"
# The real library, so the scan writes its textfile under TEXTFILE_DIR rather than the host's
# own directory, which node_exporter scrapes.
REAL_CHECK_LIB = "/usr/local/lib/homelab-check-lib.sh"
CHECK_LIB = ROLES / "setup" / "initial_setup" / "files" / "homelab-check-lib.sh"
REAL_ENV = "/etc/homelab/rkhunter-kuma-push.env"
REAL_LOG = "RKHUNTER_LOG=/var/log/rkhunter.log"
REAL_RKHUNTER = "/usr/bin/nice -n19 /usr/bin/ionice -c3 /usr/bin/rkhunter"

# The account-change warning daniel-box logged the week the claude user was added.
WARNINGS = """\
[02:02:01]   Checking for passwd file changes                 [ Warning ]
[02:02:01] Warning: User 'claude' has been added to the passwd file.
"""


def _log(rootkits: str = "0", suspect: str = "0", warnings: str = "") -> str:
    """The tail of /var/log/rkhunter.log, shaped as daniel-server wrote it on 2026-09-30."""
    return (
        warnings
        + "[02:02:01] System checks summary\n"
        + "[02:02:01] =====================\n"
        + "[02:02:01] File properties checks...\n"
        + "[02:02:01] Files checked: 142\n"
        + (f"[02:02:01] Suspect files: {suspect}\n" if suspect else "")
        + "[02:02:01] Rootkit checks...\n"
        + "[02:02:01] Rootkits checked : 498\n"
        + (f"[02:02:01] Possible rootkits: {rootkits}\n" if rootkits else "")
        + "[02:02:01] The system checks took: 1 minute and 57 seconds\n"
        + "[02:02:01] Info: End date is Wed Sep 30 02:02:01 AM UTC 2026\n"
    )


STDOUT = "Warning: User 'claude' has been added to the passwd file.\n"
ERROR_STDOUT = "Invalid option specified: --bogus\n"


def _run_check(
    tmp_path,
    stdout: str,
    rc: int,
    log: str | None,
    token: str = "stubtoken",
    stale_log: str | None = None,
):
    """Render rkhunter-check.sh and run it with rkhunter stubbed.

    The stub prints `stdout`, writes `log` to the log path when it is not None, and exits `rc`.
    `stale_log` is a log already on disk from the previous week's run, the one a failed run leaves behind. Only
    absolute paths are repointed. Returns (exit code, pushed lines or None, logged lines,
    whether rkhunter ran).
    """
    body = render_shell_script(
        *SCRIPT,
        overrides={"domain": "example.test", "k3s_metallb_ingress_vip": "10.0.0.240"},
    )
    for needle in (REAL_LIB, REAL_ENV, REAL_LOG, REAL_RKHUNTER, REAL_CHECK_LIB):
        assert needle in body, (
            f"{needle} is no longer in rkhunter-check.sh; this harness repoints it"
        )

    lib = tmp_path / "kuma-push-lib.sh"
    lib.write_text(
        'kuma_push() { printf \'%s\\n%s\\n\' "$1" "$2" > "$KUMA_PUSH_OUT"; }\n'
    )
    env_file = tmp_path / "push.env"
    env_file.write_text(f"RKHUNTER_PUSH_TOKEN={token}\n")
    log_path = tmp_path / "rkhunter.log"
    if stale_log is not None:
        log_path.write_text(stale_log)
        os.utime(log_path, (LAST_WEEK, LAST_WEEK))
    (tmp_path / "stdout").write_text(stdout)
    write_log = ""
    if log is not None:
        (tmp_path / "log-src").write_text(log)
        write_log = f"cp {tmp_path / 'log-src'} {log_path}\n"
    rkhunter = write_exec(
        tmp_path / "rkhunter",
        f'touch "$RKH_RAN"\n{write_log}cat {tmp_path / "stdout"}\nexit {rc}\n',
    )
    body = (
        body.replace(REAL_LIB, str(lib))
        .replace(REAL_CHECK_LIB, str(CHECK_LIB))
        .replace(REAL_ENV, str(env_file))
        .replace(REAL_LOG, f"RKHUNTER_LOG={log_path}")
        .replace(REAL_RKHUNTER, str(rkhunter))
    )
    script = write_exec(tmp_path / "rkhunter-check.sh", body)
    pushed, logged, ran = (tmp_path / n for n in ("pushed", "logged", "ran"))
    textfile_dir = tmp_path / "textfile"
    textfile_dir.mkdir(exist_ok=True)
    result = run(
        [str(script)],
        env={
            **os.environ,
            "KUMA_PUSH_OUT": str(pushed),
            "TEXTFILE_DIR": str(textfile_dir),
            "LOGGER_OUT": str(logged),
            "RKH_RAN": str(ran),
        },
        stub_bin=fake_bin(tmp_path / "bin", logger=LOGGER_STUB),
    )
    return (
        result.returncode,
        pushed.read_text().splitlines() if pushed.exists() else None,
        logged.read_text().splitlines() if logged.exists() else [],
        ran.exists(),
    )


def test_a_clean_scan_pushes_up(tmp_path):
    """The accept half of the DOWN arms below: a scan with nothing to report must not page.

    It passes on the pre-#4638 script too, by design. It goes red if a new DOWN arm misfires on
    a clean log, such as the Suspect files count check or the first-warning lookup. Whether a
    real host's routine warnings are suppressed is rkhunter.conf.local's job, and a stubbed
    rkhunter cannot exercise it. Only a real scan on a host after the apply can.
    """
    code, pushed, _, _ = _run_check(tmp_path, "", rc=0, log=_log())
    assert pushed[0] == "up", pushed
    assert "0 warnings, 0 suspect files, 0 possible rootkits (exit 0" in pushed[1], (
        pushed[1]
    )
    assert code == 0


def test_a_warning_pushes_down_naming_the_first(tmp_path):
    """rkhunter exits 1 on a warning, and the scan still completed."""
    code, pushed, logged, _ = _run_check(
        tmp_path, STDOUT, rc=1, log=_log(warnings=WARNINGS)
    )
    assert pushed[0] == "down", pushed
    assert "1 warnings, 0 suspect files, 0 possible rootkits (exit 1" in pushed[1]
    assert "User 'claude' has been added to the passwd file." in pushed[1], pushed[1]
    assert code == 1
    assert (
        "rkhunter: Warning: User 'claude' has been added to the passwd file." in logged
    )


def test_each_verdict_is_written_to_the_textfile_with_the_tiles_deadline(tmp_path):
    """The liveness path that outlives Kuma (#6168): clean writes 1, a warning writes 0."""
    for name, rc, log, success in (
        ("clean", 0, _log(), "1"),
        ("warning", 1, _log(warnings=WARNINGS), "0"),
    ):
        root = tmp_path / name
        root.mkdir()
        _run_check(root, "", rc=rc, log=log)
        series = _textfile(root, "rkhunter-check")
        assert series["homelab_check_success"] == success, name
        assert series["homelab_check_deadline_seconds"] == "612000"


def test_a_suspect_file_pushes_down(tmp_path):
    _, pushed, _, _ = _run_check(tmp_path, "", rc=1, log=_log(suspect="1"))
    assert pushed[0] == "down", pushed
    assert "suspect file found" in pushed[1], pushed[1]


def test_an_error_exit_that_writes_no_log_pushes_down(tmp_path):
    code, pushed, logged, _ = _run_check(tmp_path, ERROR_STDOUT, rc=1, log=None)
    assert pushed[0] == "down", pushed
    assert "rkhunter exited 1" in pushed[1]
    assert "Invalid option specified: --bogus" in pushed[1], pushed[1]
    assert code == 1
    # The durable record `probe.py alerts` reconstructs the episode from.
    assert any(line.startswith("rkhunter-check: status=down ") for line in logged), (
        logged
    )


def test_last_weeks_log_does_not_pass_for_this_run(tmp_path):
    """A run that dies before rkhunter rewrites its log leaves last week's complete summary."""
    code, pushed, _, _ = _run_check(
        tmp_path, ERROR_STDOUT, rc=1, log=None, stale_log=_log()
    )
    assert pushed[0] == "down", pushed
    assert "wrote no summary" in pushed[1], pushed[1]
    assert code == 1


def test_a_possible_rootkit_pushes_down(tmp_path):
    code, pushed, _, _ = _run_check(
        tmp_path, STDOUT, rc=1, log=_log(rootkits="1", warnings=WARNINGS)
    )
    assert pushed[0] == "down", pushed
    assert "possible rootkit found" in pushed[1], pushed[1]
    assert code == 1


def test_a_summary_missing_a_count_pushes_down(tmp_path):
    """A count the tile pages on is unreadable, so the scan cannot read as clean."""
    for missing in ({"rootkits": ""}, {"suspect": ""}):
        _, pushed, _, _ = _run_check(tmp_path, "", rc=0, log=_log(**missing))
        assert pushed[0] == "down", (missing, pushed)
        assert "lacks a Possible rootkits or Suspect files count" in pushed[1], pushed[
            1
        ]


def test_the_ssh_root_setting_follows_the_main_sshd_config():
    """rkhunter reads PermitRootLogin from the main sshd_config only (#4638).

    daniel-box's main file carries the line and daniel-pi's does not, though both harden a
    drop-in, so the setting follows what the play read rather than the host's config path.
    """
    task = next(
        task
        for task in walk_tasks(load_tasks(INTEGRITY))
        if (task.get("ansible.builtin.template") or {}).get("src") == CONF_LOCAL
    )
    (name, expression), *_ = task["vars"].items()
    for rc, expected in ((0, "no"), (1, "unset")):
        # grep's exit code, as the register before this task records it.
        value = jinja2.Template(expression).render(
            initial_setup_rkhunter_sshd_root_login={"rc": rc}
        )
        rendered = render_setup_text("initial_setup", CONF_LOCAL, {name: value})
        assert f"\nALLOW_SSH_ROOT_USER={expected}\n" in rendered, (rc, rendered)


def test_the_scan_still_runs_when_no_token_is_set(tmp_path):
    """A host whose secret is missing keeps its weekly scan; only the push is skipped."""
    _, pushed, logged, ran = _run_check(tmp_path, "", rc=0, log=_log(), token="")
    assert ran, "rkhunter did not run"
    assert pushed is None, "pushed with no token"
    assert any("RKHUNTER_PUSH_TOKEN not set" in line for line in logged), logged


def test_each_host_pushes_its_own_token():
    """Rendered once per host with a different sentinel behind each variable (#3202)."""
    sentinels = {var: f"{host}-sentinel" for host, (_, var) in TILES.items()}
    for host in TILES:
        rendered = render_setup_text(
            "initial_setup", PUSH_ENV, {"inventory_hostname": host, **sentinels}
        )
        carried = [s for s in sentinels.values() if s in rendered]
        assert carried == [f"{host}-sentinel"], (
            f"{host} must carry its own token and no other; it carries {carried}"
        )


def test_each_host_has_a_tile_gated_on_its_own_token():
    """An ungated tile sits red from creation until its secret exists."""
    for host, (tile, gate_var) in TILES.items():
        assert entity(tile, gate_var)["name"] == f"rkhunter Scan ({host})"
        assert tile_is_gated_on(tile, gate_var), (
            f"{tile} renders with {gate_var} empty, so it sits red from creation"
        )


def test_the_push_deadline_covers_one_week_and_not_two():
    cron = next(
        task["ansible.builtin.cron"]
        for task in walk_tasks(load_tasks(INTEGRITY))
        if (task.get("ansible.builtin.cron") or {}).get("name") == CRON_NAME
    )
    assert cron["weekday"] == "3" and cron["hour"] == "2", (
        "this test reads the cron as weekly; give it the new period if that changes"
    )
    assert cron["job"].startswith("/usr/local/bin/rkhunter-check.sh "), cron["job"]
    for tile, gate_var in TILES.values():
        interval = entity(tile, gate_var)["interval"]
        assert WEEK_S < interval < 2 * WEEK_S, (
            f"{tile}'s deadline {interval}s must exceed the cron's weekly period, and stay "
            "under two so a second missed run cannot pass unreported"
        )
