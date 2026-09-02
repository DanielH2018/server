"""What gitops_deploy.py execs, and how it stops what it exec'd.

The one file that IMPORTS gitops_deploy.py: it stubs host_lib.parse_env_file before the
import so the module-level `C = cfg()` reads canned values in CI and on a host alike.
`deploy_k8s()`'s argv is pinned byte-for-byte, the rollback call site in main() is pinned at
the AST to pass the FAILED commit's short SHA under its own timeout, and `run()`'s timeout
must kill the whole process group or a wedged ansible-playbook outlives the tick.
"""

# ansible/roles/setup/gitops_deploy/files/test_gitops_deploy_subprocess.py

import ast
import os
import subprocess
import sys
import time

import pytest


# ── deploy_k8s ────────────────────────────────────────────────────────────────────────────────
# gitops_deploy.py reads /etc/gitops-deploy/config.env at import time (`C = cfg()`), which
# doesn't exist in CI — see test_gitops_deploy_main_guards.py's docstring, which is why every
# other guard on that module is an AST source check rather than an import. Stub host_lib.parse_env_file
# with canned values BEFORE the only import of gitops_deploy in this file, so the import behaves
# identically in CI and on a host where the real config.env exists, and this suite never reads
# the real secrets file (forbidden — it's SOPS-managed, see the role CLAUDE.md).
def _import_gitops_deploy():
    import host_lib

    real_parse_env_file = host_lib.parse_env_file
    host_lib.parse_env_file = lambda _path: {
        "REPO_DIR": "/tmp/gitops-test-repo",
        "HOSTNAME": "test-host",
        "DISCORD_WEBHOOK": "https://discord.example/webhook",
    }
    try:
        sys.modules.pop("gitops_deploy", None)
        import gitops_deploy
    finally:
        host_lib.parse_env_file = real_parse_env_file
    return gitops_deploy


gitops_deploy = _import_gitops_deploy()


def _capture_run(monkeypatch):
    """Patch gitops_deploy.run() to record every call instead of shelling out, and return the
    list it appends to."""

    class _Call:
        def __init__(self, argv, kwargs):
            self.argv = argv
            self.kwargs = kwargs

    calls: list[_Call] = []

    def _fake_run(argv, **kwargs):
        calls.append(_Call(argv, kwargs))
        return ""

    monkeypatch.setattr(gitops_deploy, "run", _fake_run)
    return calls


_FORWARD_ARGV = [
    "uv",
    "run",
    "--frozen",
    "ansible-playbook",
    "ansible/deploy.yml",
    "--tags",
    "sonarr",
]


def test_deploy_k8s_passes_no_extra_vars_by_default(monkeypatch) -> None:
    """The ordinary deploy must be byte-identical to what it was before this slice. ~50
    services go through this call on every tick. Pins the full argv, not just -e's absence —
    a stray extra arg anywhere else in the list would pass a presence-only check."""
    calls = _capture_run(monkeypatch)
    gitops_deploy.deploy_k8s({"sonarr"}, 900.0)
    assert calls[0].argv == _FORWARD_ARGV


def test_deploy_k8s_passes_the_restore_sha_when_given(monkeypatch) -> None:
    calls = _capture_run(monkeypatch)
    gitops_deploy.deploy_k8s({"sonarr"}, 900.0, restore_sha="deadbeef")
    assert calls[0].argv == _FORWARD_ARGV + ["-e", "k8s_restore_snapshot_sha=deadbeef"]


def test_deploy_k8s_treats_a_whitespace_only_restore_sha_as_absent(monkeypatch) -> None:
    """restore_sha="" or all-whitespace must stay inert, matching the manifests role's own
    `| trim | length > 0` guard — a blank-but-truthy string must not add a broken `-e` arg."""
    calls = _capture_run(monkeypatch)
    gitops_deploy.deploy_k8s({"sonarr"}, 900.0, restore_sha="   ")
    assert calls[0].argv == _FORWARD_ARGV


# ── the rollback call site in main() ─────────────────────────────────────────────────────────
# main() shells out to git, queries GitHub for a CI verdict over HTTP, posts to Discord, and
# touches several state files under /var/lib/gitops-deploy — exercising it end-to-end would mean
# mocking all of that for one two-line assertion. This parses the ACTUAL call arguments Python
# executes (not comment text), the same AST-source-guard shape test_gitops_deploy_main_guards.py
# already uses for the rest of this un-importable-in-CI module.
def _deploy_k8s_calls_in_main(main_fn: ast.FunctionDef) -> list[ast.Call]:
    return [
        n
        for n in ast.walk(main_fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "deploy_k8s"
    ]


def test_the_rollback_redeploy_passes_the_FAILED_sha_not_the_good_one(gitops_fn):
    """The snapshot worth reverting to was taken before the failed deploy, so it is named for
    `origin` — the commit being rolled back FROM. Passing `local` here would look correct, find
    no snapshot for a first-time rollback, and fail the deploy; worse, on a second rollback of
    the same service it would find a stale snapshot and revert to the wrong point."""
    calls = _deploy_k8s_calls_in_main(gitops_fn("main"))
    assert len(calls) == 2, (
        "expected exactly one forward deploy_k8s call and one rollback redeploy in main()"
    )
    forward, rollback = calls

    forward_kwargs = {kw.arg for kw in forward.keywords}
    assert "restore_sha" not in forward_kwargs, (
        "the forward deploy must not pass restore_sha"
    )

    rollback_kwargs = {kw.arg: kw.value for kw in rollback.keywords}
    assert "restore_sha" in rollback_kwargs, (
        "the rollback redeploy must pass restore_sha"
    )
    # Pin the exact expression, not a prefix: `startswith("origin")` alone is satisfied by the
    # full 40-char `origin` (volume-snapshot names with `git rev-parse --short=8`, so a 40-char
    # SHA matches no snapshot and the revert silently never runs) and by an unrelated
    # `origin_decoy` variable — neither is what this call site must send.
    sha_expr = ast.unparse(rollback_kwargs["restore_sha"])
    assert sha_expr == "origin[:8]", (
        f"rollback restore_sha must be exactly `origin[:8]` — the FAILED commit's short SHA, "
        f"matching how volume-snapshot names its snapshots — got `{sha_expr}`"
    )


def test_the_rollback_redeploy_uses_its_own_timeout_budget(gitops_fn):
    """Task 4's addendum: give the rollback redeploy a distinct budget rather than sharing
    K8S_DEPLOY_TIMEOUT_S, since it does strictly more work than the forward deploy."""
    forward, rollback = _deploy_k8s_calls_in_main(gitops_fn("main"))
    assert ast.unparse(forward.args[1]) == "K8S_DEPLOY_TIMEOUT_S"
    assert ast.unparse(rollback.args[1]) == "K8S_ROLLBACK_TIMEOUT_S"


# ── run()'s timeout must kill the whole process group ───────────────────────────────────────────
# `uv run ansible-playbook ...` is a GRANDCHILD of run()'s subprocess (uv forks it rather than
# exec'ing into it). `subprocess.run(timeout=)` DOES return promptly on timeout — its internal
# communicate() raises on the wall-clock deadline, not on pipe EOF — but it kills only the DIRECT
# child (uv). Verified empirically against the pre-fix implementation: the call returns on time
# and the grandchild is still alive at that moment, left running as an orphan with nothing
# watching it. That is how K8S_ROLLBACK_TIMEOUT_S stopped being an actual bound on the underlying
# ansible-playbook: gitops_deploy.py moves on while the timed-out run keeps mutating the cluster,
# and the real stop becomes systemd's TimeoutStartSec SIGTERM against the wrapping unit, which can
# land mid-rollback. This shape reproduces it directly: a shell script backgrounds a grandchild
# that outlives a naive kill-the-direct-child-only fix, so the test fails against the OLD run()
# and passes only once the whole process group is killed.
_GRANDCHILD_SHAPE = """#!/bin/sh
sh -c 'echo $$ > "{pidfile}"; sleep 30' &
wait
"""


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_run_timeout_kills_the_whole_process_group(tmp_path) -> None:
    pidfile = tmp_path / "grandchild.pid"
    script = tmp_path / "parent.sh"
    script.write_text(_GRANDCHILD_SHAPE.format(pidfile=pidfile))
    script.chmod(0o755)

    start = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        gitops_deploy.run(["sh", str(script)], cwd=str(tmp_path), timeout=1.0)
    elapsed = time.monotonic() - start

    # Both the buggy and the fixed run() return around the 1.0s deadline — the deadline is a
    # wall-clock check inside communicate(), not a wait for pipe EOF, so this alone does not
    # discriminate them. It is here as a sanity bound; the real regression check is the
    # grandchild-liveness assert below.
    assert elapsed < 10, (
        f"run() took {elapsed:.1f}s to return after a 1.0s timeout — expected it to return "
        f"around the deadline regardless of whether the fix is applied"
    )

    deadline = time.monotonic() + 2
    grandchild_pid = None
    while grandchild_pid is None and time.monotonic() < deadline:
        if pidfile.exists():
            grandchild_pid = int(pidfile.read_text().strip())
        else:
            time.sleep(0.05)
    assert grandchild_pid is not None, "the grandchild never started"

    # SIGKILL is instant but reaping is not: once its own parent (the script) is also killed,
    # the grandchild is reparented and reaped by the nearest subreaper — poll briefly instead
    # of asserting the instant killpg returns.
    deadline = time.monotonic() + 3
    while _pid_is_alive(grandchild_pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _pid_is_alive(grandchild_pid), (
        f"grandchild pid {grandchild_pid} outlived the timeout — only the direct child was "
        f"killed, not its process group"
    )
