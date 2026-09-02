"""The shared gh runner: prompts and the update notifier are off, JSON is parsed."""

from __future__ import annotations

import json
import subprocess

import gh as ghmod


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
    try:
        ghmod.gh("auth", "status")
    except subprocess.CalledProcessError as exc:
        assert "not logged in" in exc.stderr
    else:
        raise AssertionError("expected CalledProcessError")
    assert ghmod.gh("auth", "status", check=False).returncode == 1
