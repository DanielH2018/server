"""The weekly worktree sweep tells Healthchecks.io its exit code, and reaps on one host.

The pruner is the dotfiles repo's `~/.claude/hooks/prune-worktrees.py`. It exits 1 when a
removal or a branch deletion failed, and exits another non-zero code when the host's copy is
missing or older than this repo needs. The exit code reaches anyone only through the ping
URL. These tests run the rendered `worktree-sweep` script of the `worktree-sweep` component
with the pruner, `uv`, `curl` and `hostname` stubbed, so they read what the timer runs rather
than a pattern over its text. The key arrives as systemd hands it over: a
`healthchecks_ping_key` file under `$CREDENTIALS_DIRECTORY`.

Run: uv run pytest hosts/tests/repo_crons/test_worktree_sweep_cron_reports_its_exit.py
"""

import os
import subprocess
from pathlib import Path

from lib._claude_worktree import REQUIRED_INTERFACE
from _shell_render import render_shell_script
from lib.git_testing import commit, git, init_repo, scrubbed_env
from lib.proc_testing import fake_bin, run, write_exec
from lib.worktree_testing import PACKAGE, ignore_foreign_slice_holds_env

SYS_USER = "sweepuser"
REMOVED_ONE = "PRUNE-RESULT: removed=1 failed=0 in_use=0 branches=0"
REMOVED_NONE = "PRUNE-RESULT: removed=0 failed=0 in_use=0 branches=0"


def _run(
    tmp_path: Path,
    sweep_rc: int,
    key: str = "k3y",
    host: str = "daniel-box",
    pruner: str | None = "",
    said: str = REMOVED_NONE,
    reap_rc: int = 0,
    prune_rc: int = 0,
    prune_said: str = "Removed 7 files (1.0MiB)",
    env: dict[str, str] | None = None,
    policy: bool = True,
    user: str = "home",
):
    """Run `host`'s script with the pruner exiting `sweep_rc`; return its result and the ping URL.

    The pruner stub writes its arguments to `pruner-args` under `tmp_path`, prints `said`
    and exits `sweep_rc`. A `pruner` body replaces that stub, and None leaves the host with
    no pruner at all. The `uv` stub appends each call's arguments to `uv-calls`. For `uv cache`
    it prints `prune_said` and exits `prune_rc`, and for anything else it exits `reap_rc`.
    `env` replaces the inherited environment. `policy=False` leaves the checkout without
    `.claude/prune-worktrees.json`, as a checkout from before that file was added has.
    The script runs with `HOME` at `tmp_path / user`, and `tmp_path / "home"` stands in for
    sys_user's home, so another `user` runs it as the agent does.
    """
    home = tmp_path / user
    (home / "server").mkdir(parents=True, exist_ok=True)
    if policy:
        (home / "server/.claude").mkdir(exist_ok=True)
        (home / "server/.claude/prune-worktrees.json").write_text("{}\n")
    if pruner is not None:
        write_exec(
            home / ".claude/hooks/prune-worktrees.py",
            pruner
            or f'printf "%s\\n" "$@" > {tmp_path / "pruner-args"}\n'
            f'echo "{said}"\nexit {sweep_rc}\n',
        )
    write_exec(
        home / ".local/bin/uv",
        f'echo "$*" >> {tmp_path / "uv-calls"}\n'
        f"if [ \"$1\" = cache ]; then echo '{prune_said}'; exit {prune_rc}; fi\n"
        f"exit {reap_rc}\n",
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
    body = body.replace(f"/home/{SYS_USER}", str(tmp_path / "home"))
    # The script pins PATH, so the stubbed curl and hostname go in front of it there.
    body = body.replace("export PATH=", f"export PATH={stubs}:")
    assert "{{" not in body, body
    script = tmp_path / "worktree-sweep"
    script.write_text(body)
    env = {
        **(os.environ if env is None else env),
        "CREDENTIALS_DIRECTORY": str(credentials),
        "HOME": str(home),
    }
    result = run(["bash", str(script)], env=env)
    return result, pinged.read_text().strip() if pinged.exists() else ""


def _uv_calls(tmp_path: Path) -> list[list[str]]:
    calls = tmp_path / "uv-calls"
    if not calls.exists():
        return []
    return [line.split() for line in calls.read_text().splitlines()]


def _reaped(tmp_path: Path) -> list[str] | None:
    return next((call for call in _uv_calls(tmp_path) if call[0] == "run"), None)


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


def test_the_sweep_requires_the_interface_this_repo_was_written_for(tmp_path):
    # A host whose `chezmoi apply` is behind must fail the run, not sweep by older rules.
    _run(tmp_path, 0)
    args = (tmp_path / "pruner-args").read_text().split()
    assert args == ["--require", str(REQUIRED_INTERFACE), "--prune", "--brief"]


def test_a_host_with_no_pruner_fails_the_run_and_pings_the_failure(tmp_path):
    # The RED half of "nothing to remove": a missing copy must never read as a clean sweep.
    result, url = _run(tmp_path, 0, pruner=None)

    assert result.returncode == 127
    assert url == "https://hc-ping.com/k3y/daniel-box-worktree-sweep/127"


def test_a_checkout_with_no_policy_file_fails_the_run_and_pings_the_failure(tmp_path):
    # fact: hosts/components/worktree-sweep/CLAUDE.md#Autonomous-role contract (the timer removes worktrees and branches with no human in the loop)
    # Without the policy the pruner keeps every fan-out tree and exits 0, a false "nothing to remove".
    result, url = _run(tmp_path, 0, policy=False)

    assert not (tmp_path / "pruner-args").exists()
    assert "no .claude/prune-worktrees.json" in result.stdout
    assert result.returncode == 66
    assert url == "https://hc-ping.com/k3y/daniel-box-worktree-sweep/66"


def test_the_ops_cron_host_reaps_claims_after_a_removal(tmp_path):
    # A removed tree's claims stay on the register until `reap` runs.
    _run(tmp_path, 0, said=REMOVED_ONE)
    assert _reaped(tmp_path) == [
        "run",
        "--quiet",
        "python",
        "scripts/dev/findings.py",
        "reap",
    ]


def test_the_ops_cron_host_does_not_reap_when_nothing_was_removed(tmp_path):
    _run(tmp_path, 0, said=REMOVED_NONE)
    assert _reaped(tmp_path) is None


def test_a_sweep_on_another_host_does_not_reap_claims(tmp_path):
    # Its reap would read every claim the daniel-box orchestrator holds as stale.
    _run(tmp_path, 0, host="daniel-server", said=REMOVED_ONE)
    assert _reaped(tmp_path) is None


def test_a_sweep_as_the_agent_prunes_its_own_clone_and_does_not_reap(tmp_path):
    # The agent's clone cannot see the operator's fan-out trees, so its reap would free live claims.
    result, _ = _run(tmp_path, 0, said=REMOVED_ONE, user="agent")

    assert result.returncode == 0
    assert (tmp_path / "pruner-args").exists()
    assert not (tmp_path / "home").exists()
    assert _reaped(tmp_path) is None


def test_a_reap_that_fails_is_named_and_leaves_the_sweeps_exit_code(tmp_path):
    result, url = _run(tmp_path, 0, said=REMOVED_ONE, reap_rc=3)
    assert "claims not reaped: reap exited 3" in result.stdout
    assert result.returncode == 0
    assert url == "https://hc-ping.com/k3y/daniel-box-worktree-sweep/0"


def test_the_sweep_prunes_the_uv_cache_and_names_what_it_removed(tmp_path):
    result, url = _run(tmp_path, 0)

    assert ["cache", "prune"] in _uv_calls(tmp_path)
    assert "uv cache: Removed 7 files (1.0MiB)" in result.stdout
    assert url == "https://hc-ping.com/k3y/daniel-box-worktree-sweep/0"


def test_a_failed_uv_cache_prune_is_flagged_and_fails_the_run(tmp_path):
    # fact: hosts/components/worktree-sweep/CLAUDE.md#Autonomous-role contract (the timer removes worktrees and branches with no human in the loop)
    result, url = _run(tmp_path, 0, prune_rc=2)

    assert "uv cache not pruned: uv cache prune exited 2" in result.stdout
    assert result.returncode == 2
    assert url == "https://hc-ping.com/k3y/daniel-box-worktree-sweep/2"


def test_a_prune_a_running_uv_held_off_is_skipped_and_leaves_the_exit_code(tmp_path):
    # fact: hosts/components/worktree-sweep/CLAUDE.md#Autonomous-role contract (the timer removes worktrees and branches with no human in the loop)
    # A fan-out review's `uv run` holds the cache's shared lock for hours.
    held = (
        "error: Timeout (300s) when waiting for lock on `/home/u/.cache/uv` at "
        "`.cache/uv/.lock`, is another uv process running?"
    )
    result, url = _run(tmp_path, 0, prune_rc=2, prune_said=held)

    assert "uv cache not pruned this week" in result.stdout
    assert result.returncode == 0
    assert url == "https://hc-ping.com/k3y/daniel-box-worktree-sweep/0"


def _launcher() -> Path:
    """The dotfiles pruner's entry point: in the package's source tree, or deployed."""
    source = (
        PACKAGE.parents[2] / "private_dot_claude/hooks/executable_prune-worktrees.py"
    )
    if source.is_file():
        return source
    return Path.home() / ".claude" / "hooks" / "prune-worktrees.py"


def _own_processes_only(tmp_path: Path) -> dict[str, str]:
    """The environment that switches the slice rule off in the pruner the script starts.

    `ignore_foreign_slice_holds_env` writes a `claude_worktree` that loads the real one. The
    launcher imports the sweep module from the same directory, so it is linked in beside it.
    """
    env = ignore_foreign_slice_holds_env(tmp_path)
    home = Path(env["CLAUDE_WORKTREE_HOME"])
    (home / "claude_worktree_sweep.py").symlink_to(PACKAGE / "claude_worktree_sweep.py")
    return env


def test_a_sweep_that_skips_a_worktree_a_live_process_holds_pings_0(tmp_path):
    # The real pruner over a checkout whose one merged worktree a live process holds.
    server = tmp_path / "home" / "server"
    init_repo(server)
    commit(server, "init", **{"a.txt": "one\n"})
    held = server / ".claude" / "worktrees" / "held"
    config = tmp_path / "home" / ".claude"
    held.parent.mkdir(parents=True)
    git(server, "worktree", "add", "-q", "-b", "worktree-held", str(held))
    landed = commit(held, "landed work", **{"b.txt": "two\n"})
    git(server, "update-ref", "refs/remotes/origin/master", landed)
    holder = subprocess.Popen(["sleep", "120"], cwd=held)
    try:
        result, url = _run(
            tmp_path,
            0,
            pruner=f'exec python3 {_launcher()} "$@"\n',
            env=scrubbed_env(
                GH_BIN="true",
                CLAUDE_CONFIG_DIR=str(config),
                **_own_processes_only(tmp_path),
            ),
        )
    finally:
        holder.kill()
        holder.wait()

    assert f"in use by a live process: pid {holder.pid}" in result.stderr
    assert held.is_dir()
    assert str(held) in (config / "logs/sessions.log").read_text()
    assert url == "https://hc-ping.com/k3y/daniel-box-worktree-sweep/0", result.stderr
