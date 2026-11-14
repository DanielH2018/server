"""The shared gh runner: prompts and the update notifier are off, JSON is parsed."""

import json
import subprocess

import gh as ghmod
import gh_quota
import pytest

# 2100-01-01, so the quota window in the ledger test stays open under the real clock.
WINDOW_END = 4_102_444_800


def test_gh_disables_prompts_and_the_notifier(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["env"] = kwargs["env"]
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(ghmod.subprocess, "run", fake_run)
    ghmod.gh("issue", "list")
    assert seen["argv"] == ["gh", "issue", "list"]
    assert seen["env"]["GH_PROMPT_DISABLED"] == "1"
    assert seen["env"]["GH_NO_UPDATE_NOTIFIER"] == "1"


def test_gh_json_parses_stdout(monkeypatch):
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps([{"number": 7}]), stderr=""
        )

    monkeypatch.setattr(ghmod.subprocess, "run", fake_run)
    assert ghmod.gh_json("issue", "list", "--json", "number") == [{"number": 7}]


def test_gh_json_empty_stdout_is_none(monkeypatch):
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(ghmod.subprocess, "run", fake_run)
    assert ghmod.gh_json("api", "x") is None


def test_gh_nonzero_raises_by_default(monkeypatch):
    def fake_run(argv, **kwargs):
        if kwargs.get("check"):
            raise subprocess.CalledProcessError(1, argv, stderr="not logged in")
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="not logged in")

    monkeypatch.setattr(ghmod.subprocess, "run", fake_run)
    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        ghmod.gh("auth", "status")
    assert "not logged in" in excinfo.value.stderr
    assert ghmod.gh("auth", "status", check=False).returncode == 1


def test_gh_counts_the_call_against_the_session_before_it_runs(monkeypatch, tmp_path):
    monkeypatch.setenv("GH_QUOTA_MODE", "record")
    monkeypatch.setenv("GH_QUOTA_DIR", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "session-under-test")
    resources = {
        "graphql": {"used": 40, "limit": 5000, "reset": WINDOW_END},
        "core": {"used": 0, "limit": 5000, "reset": WINDOW_END},
    }

    def sample(argv, **_kwargs):
        return subprocess.CompletedProcess(
            argv, 0, json.dumps({"resources": resources}), ""
        )

    # Reading the ledger first takes the sample here, so `gh()` below does not run one.
    ledger = gh_quota.Ledger(root=tmp_path, run=sample)
    (window,) = ledger.windows()
    assert window.sessions == {}

    # A missing cwd stops subprocess before `gh` starts, so nothing reaches GitHub.
    with pytest.raises(FileNotFoundError):
        ghmod.gh("pr", "list", "--state", "open", cwd=tmp_path / "missing")

    (window,) = ledger.windows()
    assert window.sessions == {"session-under-test": {"pr list": 1}}
