"""The weekly worktree sweep tells Healthchecks.io its exit code.

`prune_worktrees.py` exits 1 when a removal or a branch deletion failed, and the exit code
reaches anyone only through the ping URL (#4482). These tests run the rendered
`worktree-sweep` script of the `worktree-sweep` component with `uv`, `curl` and `hostname`
stubbed, so they read what the timer runs rather than a pattern over its text. The key arrives
as systemd hands it over: a `healthchecks_ping_key` file under `$CREDENTIALS_DIRECTORY`.

Run: uv run pytest ansible/tests/setup/test_worktree_sweep_cron_reports_its_exit.py
"""

import os
import subprocess
import sys
from pathlib import Path

from _shell_render import render_shell_script
from lib.git_testing import commit, git, init_repo, scrubbed_env
from lib.proc_testing import fake_bin, run, write_exec
from lib.repo_paths import REPO

SYS_USER = "sweepuser"


def _run(
    tmp_path: Path,
    sweep_rc: int,
    key: str = "k3y",
    host: str = "daniel-box",
    uv: str | None = None,
    env: dict[str, str] | None = None,
):
    """Run `host`'s script with the sweep exiting `sweep_rc`; return its result and the ping URL.

    The `uv` stub writes the arguments it was given to `uv-args` under `tmp_path`. A `uv`
    body replaces that stub, and `env` replaces the inherited environment.
    """
    home = tmp_path / "home"
    (home / "server").mkdir(parents=True, exist_ok=True)
    write_exec(
        home / ".local/bin/uv",
        uv or f'printf "%s\\n" "$@" > {tmp_path / "uv-args"}\nexit {sweep_rc}\n',
    )
    pinged = tmp_path / "pinged"
    stubs = fake_bin(
        tmp_path / "bin",
        curl=f'for a; do last="$a"; done; echo "$last" > {pinged}\n',
        hostname="echo daniel-box\n",
    )
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    (credentials / "healthchecks_ping_key").write_text(key)
    body = render_shell_script(
        "components",
        "worktree-sweep",
        "worktree-sweep.sh.j2",
        overrides={"sys_user": SYS_USER, "inventory_hostname": host},
    )
    body = body.replace(f"/home/{SYS_USER}", str(home))
    assert "{{" not in body, body
    script = tmp_path / "worktree-sweep"
    script.write_text(body)
    env = {
        **(os.environ if env is None else env),
        "CREDENTIALS_DIRECTORY": str(credentials),
    }
    result = run(["bash", str(script)], env=env, stub_bin=stubs)
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


def _sweep_args(tmp_path: Path, host: str) -> list[str]:
    _run(tmp_path, 0, host=host)
    return (tmp_path / "uv-args").read_text().split()


def test_the_ops_cron_host_sweep_reaps_claims(tmp_path):
    args = _sweep_args(tmp_path, "daniel-box")
    assert args[-3:] == ["scripts/dev/prune_worktrees.py", "--prune", "--brief"]


def test_a_sweep_on_another_host_does_not_reap_claims(tmp_path):
    # Its reap would read every claim the daniel-box orchestrator holds as stale.
    args = _sweep_args(tmp_path, "daniel-server")
    assert args[-4:] == [
        "scripts/dev/prune_worktrees.py",
        "--prune",
        "--brief",
        "--no-reap",
    ]


def test_a_sweep_that_skips_a_worktree_a_live_process_holds_pings_0(tmp_path):
    # #7949: the real pruner over a checkout whose one merged worktree a live process holds.
    server = tmp_path / "home" / "server"
    init_repo(server)
    commit(server, "init", **{"a.txt": "one\n"})
    held = tmp_path / "held"
    git(server, "worktree", "add", "-q", "-b", "worktree-held", str(held))
    git(server, "update-ref", "refs/remotes/origin/master", "master")
    pruner = REPO / "scripts" / "dev" / "prune_worktrees.py"
    uv = f'shift 4\nexec {sys.executable} {pruner} "$@"\n'
    holder = subprocess.Popen(["sleep", "120"], cwd=held)
    try:
        result, url = _run(tmp_path, 0, uv=uv, env=scrubbed_env())
    finally:
        holder.kill()
        holder.wait()

    assert f"in use by a live process: pid {holder.pid}" in result.stderr
    assert url == "https://hc-ping.com/k3y/daniel-box-worktree-sweep/0", result.stderr
