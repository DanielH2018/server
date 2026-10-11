"""The weekly worktree sweep tells Healthchecks.io its exit code, through the `logger` pipe.

The job pipes into `logger`, so the job's own exit status is logger's and says nothing about
the sweep. The sweep's exit code reaches anyone only through the ping URL (#4482). These tests
run the installed job string under /bin/sh with `uv`, `curl` and `logger` stubbed, so they read
what cron runs rather than a pattern over its text.

Run: uv run pytest ansible/tests/setup/test_worktree_sweep_cron_reports_its_exit.py
"""

from pathlib import Path

from _helpers import load_tasks, walk_tasks
from _role_census import task_files
from lib.proc_testing import fake_bin, run, write_exec

CRON_NAME = "Weekly git object-store repair"


def _job() -> str:
    for path in task_files():
        for task in walk_tasks(load_tasks(path)):
            cron = task.get("ansible.builtin.cron")
            if isinstance(cron, dict) and cron.get("name") == CRON_NAME:
                return str(cron["job"])
    raise AssertionError(f"no cron named {CRON_NAME!r}: did the sweep move?")


def _run(tmp_path: Path, sweep_rc: int, key: str = "k3y") -> str:
    """Run the job with the sweep exiting `sweep_rc`; return the URL curl was asked for."""
    home = tmp_path / "home"
    (home / "server").mkdir(parents=True)
    write_exec(home / ".local/bin/uv", f"exit {sweep_rc}\n")
    pinged = tmp_path / "pinged"
    stubs = fake_bin(
        tmp_path / "bin",
        curl=f'for a; do last="$a"; done; echo "$last" > {pinged}\n',
        logger="cat >/dev/null\n",
    )
    ping_env = tmp_path / "ping.env"
    ping_env.write_text(f"HC_PING_KEY='{key}'\n")
    job = (
        _job()
        .replace("/home/{{ sys_user }}", str(home))
        .replace("{{ inventory_hostname }}", "daniel-box")
        .replace("/etc/healthchecks/ping.env", str(ping_env))
    )
    assert "{{" not in job, job
    run(job, shell=True, stub_bin=stubs, check=True)
    return pinged.read_text().strip() if pinged.exists() else ""


def test_a_failed_sweep_pings_its_exit_code(tmp_path):
    assert _run(tmp_path, 1) == "https://hc-ping.com/k3y/daniel-box-worktree-sweep/1"


def test_a_clean_sweep_pings_exit_0(tmp_path):
    assert _run(tmp_path, 0) == "https://hc-ping.com/k3y/daniel-box-worktree-sweep/0"


def test_no_key_sends_no_ping(tmp_path):
    assert _run(tmp_path, 0, key="") == ""
