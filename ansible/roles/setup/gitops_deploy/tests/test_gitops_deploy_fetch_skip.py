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

import dataclasses
import subprocess

import pytest

from deploy_toolbox import DeployTools

# The SHAs _tick's `run` answers rev-parse with; `from _deploy_fakes import` is avoided for two
# constants that would otherwise pull the whole scripted tick into a module that scripts none.
LOCAL = "1" * 40
ORIGIN = "2" * 40


# ── main(): the two retryable git failures ────────────────────────────────────────────────────
def _completed(returncode: int, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout="", stderr=stderr
    )


def _tools(**overrides) -> DeployTools:
    """A DeployTools whose git reads succeed and whose posts go nowhere, plus `overrides`.

    The two disk-only steps ahead of the first git call — drain_pending() and
    check_stale_composes() — need no fake: `state_dir` repoints the queue file, and REPO names
    a checkout that does not exist, so `stale_composes` reads None and the watchdog is silent.
    """
    base = DeployTools(
        git_status=lambda _repo: _completed(0),
        git_fetch=lambda _repo, _branch: _completed(0),
        discord_post=lambda _webhook, _content: True,
    )
    return dataclasses.replace(base, **overrides)


def test_a_failing_git_status_raises_retryable(gitops_deploy, state_dir):
    # 2026-08-17 14:33: `git status --porcelain` exited 128 on a momentarily unreadable work tree
    # (a parallel `git worktree` operation), the very next tick was fine, and the tick double-paged.
    tools = _tools(
        git_status=lambda _repo: _completed(
            128, "fatal: this operation must be run in a work tree"
        )
    )
    with pytest.raises(gitops_deploy.RetryableFetchError, match="work tree"):
        gitops_deploy.main(tools)


def test_a_failing_fetch_raises_retryable(gitops_deploy, state_dir):
    # A clean status, then a fetch that fails: git_fetch is unchecked `subprocess.run`, not
    # `run()`, so the failure carries git's stderr and does not fall through run()'s RuntimeError
    # to the crash page.
    tools = _tools(
        git_fetch=lambda _repo, _branch: _completed(
            128, "fatal: unable to access 'https://github.com/'"
        )
    )
    with pytest.raises(gitops_deploy.RetryableFetchError, match="unable to access"):
        gitops_deploy.main(tools)


def test_a_missing_config_refuses_to_tick(gitops_deploy, monkeypatch, state_dir):
    # The import survives a missing config so the suite can load the module; the tick must not.
    # An empty REPO would run every git command below against cwd="" and page from somewhere
    # confusing, so main() pages from the top and names the file it could not read.
    monkeypatch.setattr(gitops_deploy, "REPO", "")
    with pytest.raises(RuntimeError, match="REPO_DIR is unset"):
        gitops_deploy.main(_tools())


# ── entrypoint(): the exit-code contract around main() ────────────────────────────────────────
def _tick(
    gitops_deploy, monkeypatch, outcome, discord_ok: bool = True
) -> tuple[DeployTools, dict[str, list]]:
    """entrypoint()'s tools, with main() replaced by `outcome`.

    `outcome` is a return value or an exception to raise. The returned dict records every
    Discord post and every git call `_record_behind` made — the real one runs, so "the marker
    was recorded after main()" is read off the git it did rather than off a patched stub.

    main() itself stays patched: it is this module's subject, not a process boundary.
    """
    seen: dict[str, list] = {"posts": [], "git": []}

    def discord_post(_webhook: str, content: str) -> bool:
        seen["posts"].append(content)
        return discord_ok

    def run(argv, **_kwargs) -> str:
        seen["git"].append(argv)
        return ORIGIN if argv[-1].startswith("origin/") else LOCAL

    def fake_main(_tools) -> int:
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(gitops_deploy, "main", fake_main)
    return _tools(
        discord_post=discord_post,
        run=run,
        is_ancestor=lambda _repo, _a, _d: True,
    ), seen


def test_a_retryable_fetch_failure_is_a_clean_skip(
    gitops_deploy, monkeypatch, state_dir
):
    tools, seen = _tick(
        gitops_deploy, monkeypatch, gitops_deploy.RetryableFetchError("blip")
    )
    # exit 0 → systemd sees success → the OnFailure alert unit does not fire
    assert gitops_deploy.entrypoint(tools) == 0
    assert seen["posts"] == [], "a retryable fetch failure must not post a crash alert"
    assert not (state_dir / "last_run").exists(), (
        "a skipped tick must not refresh last_run — a persistent fetch break would then hide "
        "behind a green GitOps-Alive"
    )
    assert seen["git"] == [], "a skipped tick must not record the behind-origin marker"


def test_an_unusable_config_is_one_line_and_exit_0_on_a_delivered_post(
    gitops_deploy, monkeypatch, state_dir, capsys
):
    """The acceptance criterion for moving the config parse out of import time.

    `HEALTH_TIMEOUT_S=5m` used to raise `ValueError: invalid literal for int()` while the module
    was still importing — no key name, no webhook, no log line. The handler must sit ABOVE the
    generic `except Exception` (ConfigError subclasses it, so a reordering silently restores the
    traceback), and must leave last_run alone: a deployer that cannot parse its config is not
    ticking, and writing the marker would hold GitOps-Alive green over it. Exit 0 on a delivered
    detailed post — same convention as every other `0 if posted else 1` branch — so OnFailure's
    generic curl doesn't double-page; see the failure-path test below for the exit-1 half.
    """
    import deploy_io

    tools, seen = _tick(
        gitops_deploy,
        monkeypatch,
        deploy_io.ConfigError(
            "unusable deployer config: HEALTH_TIMEOUT_S='5m' is not a whole number"
        ),
    )
    assert gitops_deploy.entrypoint(tools) == 0
    out = capsys.readouterr().out.strip()
    assert out.count("\n") == 0, (
        f"a diagnosable failure is one line, not a block: {out}"
    )
    assert "HEALTH_TIMEOUT_S" in out and "5m" in out
    assert len(seen["posts"]) == 1 and "HEALTH_TIMEOUT_S" in seen["posts"][0]
    assert not (state_dir / "last_run").exists()
    assert seen["git"] == [], "a skipped tick must not record the behind-origin marker"


def test_an_unusable_config_exits_1_when_the_alert_itself_cant_be_delivered(
    gitops_deploy, monkeypatch, state_dir
):
    # Red proof for the branch above: when the detailed post fails, OnFailure is the backstop.
    import deploy_io

    tools, _seen = _tick(
        gitops_deploy,
        monkeypatch,
        deploy_io.ConfigError(
            "unusable deployer config: HEALTH_TIMEOUT_S='5m' is not a whole number"
        ),
        discord_ok=False,
    )
    assert gitops_deploy.entrypoint(tools) == 1
    assert not (state_dir / "last_run").exists()


def test_a_genuine_crash_still_pages_and_reraises(
    gitops_deploy, monkeypatch, state_dir
):
    # The fix must not have silenced real crashes: page, re-raise (so OnFailure fires too), and
    # leave last_run alone so the Alive monitor also goes stale if this keeps happening.
    tools, seen = _tick(gitops_deploy, monkeypatch, ValueError("boom"))
    with pytest.raises(ValueError, match="boom"):
        gitops_deploy.entrypoint(tools)
    assert seen["posts"] == ["🚨 gitops-deploy crashed: boom"]
    assert not (state_dir / "last_run").exists()


@pytest.mark.parametrize("rc", [0, 1])
def test_a_completed_tick_writes_last_run_and_returns_mains_rc(
    gitops_deploy, monkeypatch, state_dir, rc
):
    # rc=1 is a rollback: the tick completed, so the liveness marker is written all the same.
    tools, seen = _tick(gitops_deploy, monkeypatch, rc)
    assert gitops_deploy.entrypoint(tools) == rc
    assert seen["posts"] == []
    assert [argv[1] for argv in seen["git"]] == ["rev-parse", "rev-parse"], (
        "the behind-origin marker is recorded after main()"
    )
    stamp = float((state_dir / "last_run").read_text())
    assert stamp > 0
