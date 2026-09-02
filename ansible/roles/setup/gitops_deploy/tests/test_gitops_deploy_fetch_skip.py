"""A transient git failure must be a clean skip, not a page.

main() raises RetryableFetchError when `git status` or `git fetch` fails, and entrypoint()
turns that into exit 0 with no Discord post and NO last_run write: a one-off GitHub blip is
retried invisibly next tick, while a persistent fetch break ages last_run and trips
GitOps-Alive. Any other exception still pages and re-raises, and a completed tick, rollback
included, writes last_run. Before the RetryableFetchError split, a fetch error double-paged
(the crash Discord plus the OnFailure unit) every 30 minutes for the length of a GitHub
incident. Each contract here is exercised by calling the function, against the canned config
and a tmp state dir from conftest.py.
"""

# ansible/roles/setup/gitops_deploy/tests/test_gitops_deploy_fetch_skip.py

import subprocess

import pytest


# ── main(): the two retryable git failures ────────────────────────────────────────────────────
def _completed(returncode: int, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout="", stderr=stderr
    )


def _quiet_tick_start(gitops_deploy, monkeypatch) -> None:
    """main() up to its first git call: the two disk-only steps ahead of it become no-ops."""
    monkeypatch.setattr(gitops_deploy, "drain_pending", lambda: None)
    monkeypatch.setattr(gitops_deploy, "check_stale_composes", lambda: None)


def test_a_failing_git_status_raises_retryable(gitops_deploy, monkeypatch):
    # 2026-08-17 14:33: `git status --porcelain` exited 128 on a momentarily unreadable work tree
    # (a parallel `git worktree` operation), the very next tick was fine, and the tick double-paged.
    _quiet_tick_start(gitops_deploy, monkeypatch)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_a, **_k: _completed(
            128, "fatal: this operation must be run in a work tree"
        ),
    )
    with pytest.raises(gitops_deploy.RetryableFetchError, match="work tree"):
        gitops_deploy.main()


def test_a_failing_fetch_raises_retryable(gitops_deploy, monkeypatch):
    # A clean status, then a fetch that fails: the fetch is `subprocess.run`, not `run()`, so the
    # failure carries git's stderr and does not fall through run()'s RuntimeError to the crash page.
    _quiet_tick_start(gitops_deploy, monkeypatch)

    def fake_run(argv, **_kwargs):
        if argv[:2] == ["git", "status"]:
            return _completed(0)
        assert argv[:3] == ["git", "fetch", "origin"], argv
        return _completed(128, "fatal: unable to access 'https://github.com/'")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(gitops_deploy.RetryableFetchError, match="unable to access"):
        gitops_deploy.main()


def test_a_missing_config_refuses_to_tick(gitops_deploy, monkeypatch):
    # The import survives a missing config so the suite can load the module; the tick must not.
    # An empty REPO would run every git command below against cwd="" and page from somewhere
    # confusing, so main() pages from the top and names the file it could not read.
    _quiet_tick_start(gitops_deploy, monkeypatch)
    monkeypatch.setattr(gitops_deploy, "REPO", "")
    with pytest.raises(RuntimeError, match="REPO_DIR is unset"):
        gitops_deploy.main()


# ── entrypoint(): the exit-code contract around main() ────────────────────────────────────────
def _tick(gitops_deploy, monkeypatch, outcome) -> dict[str, list]:
    """Run entrypoint() with main() replaced by `outcome` (a return value, or an exception to
    raise), recording every Discord post and whether the behind-origin marker was refreshed."""
    seen: dict[str, list] = {"posts": [], "behind": []}
    monkeypatch.setattr(
        gitops_deploy, "discord", lambda content: seen["posts"].append(content) or True
    )
    monkeypatch.setattr(
        gitops_deploy, "_record_behind", lambda: seen["behind"].append(True)
    )

    def fake_main() -> int:
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(gitops_deploy, "main", fake_main)
    return seen


def test_a_retryable_fetch_failure_is_a_clean_skip(
    gitops_deploy, monkeypatch, state_dir
):
    seen = _tick(gitops_deploy, monkeypatch, gitops_deploy.RetryableFetchError("blip"))
    # exit 0 → systemd sees success → the OnFailure alert unit does not fire
    assert gitops_deploy.entrypoint() == 0
    assert seen["posts"] == [], "a retryable fetch failure must not post a crash alert"
    assert not (state_dir / "last_run").exists(), (
        "a skipped tick must not refresh last_run — a persistent fetch break would then hide "
        "behind a green GitOps-Alive"
    )
    assert seen["behind"] == []


def test_a_genuine_crash_still_pages_and_reraises(
    gitops_deploy, monkeypatch, state_dir
):
    # The fix must not have silenced real crashes: page, re-raise (so OnFailure fires too), and
    # leave last_run alone so the Alive monitor also goes stale if this keeps happening.
    seen = _tick(gitops_deploy, monkeypatch, ValueError("boom"))
    with pytest.raises(ValueError, match="boom"):
        gitops_deploy.entrypoint()
    assert seen["posts"] == ["🚨 gitops-deploy crashed: boom"]
    assert not (state_dir / "last_run").exists()


@pytest.mark.parametrize("rc", [0, 1])
def test_a_completed_tick_writes_last_run_and_returns_mains_rc(
    gitops_deploy, monkeypatch, state_dir, rc
):
    # rc=1 is a rollback: the tick completed, so the liveness marker is written all the same.
    seen = _tick(gitops_deploy, monkeypatch, rc)
    assert gitops_deploy.entrypoint() == rc
    assert seen["posts"] == []
    assert seen["behind"] == [True], "the behind-origin marker is recorded after main()"
    stamp = float((state_dir / "last_run").read_text())
    assert stamp > 0
