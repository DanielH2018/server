"""The weekly worktree sweep tells Healthchecks.io its exit code.

`prune_worktrees.py` exits 1 when a removal or a branch deletion failed, and the exit code
reaches anyone only through the ping URL (#4482). These tests run the rendered
`worktree-sweep` script of the `worktree-sweep` component with `uv`, `curl` and `hostname`
stubbed, so they read what the timer runs rather than a pattern over its text.

Run: uv run pytest ansible/tests/setup/test_worktree_sweep_cron_reports_its_exit.py
"""

from pathlib import Path

from _shell_render import render_shell_script
from lib.proc_testing import fake_bin, run, write_exec

SYS_USER = "sweepuser"


def _run(tmp_path: Path, sweep_rc: int, key: str = "k3y"):
    """Run the script with the sweep exiting `sweep_rc`; return its result and the ping URL."""
    home = tmp_path / "home"
    (home / "server").mkdir(parents=True)
    write_exec(home / ".local/bin/uv", f"exit {sweep_rc}\n")
    pinged = tmp_path / "pinged"
    stubs = fake_bin(
        tmp_path / "bin",
        curl=f'for a; do last="$a"; done; echo "$last" > {pinged}\n',
        hostname="echo daniel-box\n",
    )
    ping_env = tmp_path / "ping.env"
    ping_env.write_text(f"HC_PING_KEY='{key}'\n")
    body = render_shell_script(
        "components",
        "worktree-sweep",
        "worktree-sweep.sh.j2",
        overrides={"sys_user": SYS_USER},
    )
    body = body.replace(f"/home/{SYS_USER}", str(home)).replace(
        "/etc/healthchecks/ping.env", str(ping_env)
    )
    assert "{{" not in body, body
    script = tmp_path / "worktree-sweep"
    script.write_text(body)
    result = run(["bash", str(script)], stub_bin=stubs)
    return result, pinged.read_text().strip() if pinged.exists() else ""


def test_a_failed_sweep_pings_its_exit_code_and_fails(tmp_path):
    result, url = _run(tmp_path, 1)
    assert url == "https://hc-ping.com/k3y/daniel-box-worktree-sweep/1"
    assert result.returncode == 1


def test_a_clean_sweep_pings_exit_0(tmp_path):
    result, url = _run(tmp_path, 0)
    assert url == "https://hc-ping.com/k3y/daniel-box-worktree-sweep/0"
    assert result.returncode == 0


def test_no_key_sends_no_ping(tmp_path):
    assert _run(tmp_path, 0, key="")[1] == ""
