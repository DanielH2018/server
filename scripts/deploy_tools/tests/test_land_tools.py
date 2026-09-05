"""The real boundary implementations build the right process, in the right checkout.

Run: uv run pytest scripts/deploy_tools/tests/test_land_tools.py
"""

import subprocess
import sys
from pathlib import Path

from deploy_tools.land_lib import tools


def _capture(monkeypatch):
    seen = {}

    def run(argv, **kw):
        seen.update(argv=list(argv), **kw)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(tools.subprocess, "run", run)
    return seen


def test_deploy_tags_is_the_relative_path_in_the_primary_checkout(monkeypatch):
    """Its question IS the primary checkout: `blockers` reads HEAD..origin/master there."""
    seen = _capture(monkeypatch)
    tools.run_deploy_tags(Path("/primary"), ["blockers", "origin/master"])
    assert seen["argv"][3] == "scripts/deploy_tools/deploy_tags.py"
    assert seen["cwd"] == Path("/primary")


def test_deploy_adds_target_only_for_a_remote_host(monkeypatch):
    seen = _capture(monkeypatch)
    tools.run_deploy(Path("/primary"), ["alloy"], "daniel-pi")
    assert seen["argv"] == [
        "./scripts/deploy.sh",
        "--tags",
        "alloy",
        "-e",
        "target=daniel-pi",
    ]
    tools.run_deploy(Path("/primary"), ["sonarr"], None)
    assert seen["argv"] == ["./scripts/deploy.sh", "--tags", "sonarr"]
    # deploy.sh renders from its working directory, so a wrong cwd would deploy the wrong
    # checkout's templates and report success.
    assert seen["cwd"] == Path("/primary")


def test_the_tick_runs_from_beside_land_py(monkeypatch):
    seen = _capture(monkeypatch)
    tools.run_tick()
    assert seen["argv"] == [str(tools.HERE / "gitops_tick.sh")]
    assert (tools.HERE / "land.py").exists() or (tools.HERE / "land.sh").exists()


def test_helpers_whose_code_must_match_are_imported_from_beside_land_py():
    """Issue #851: a helper this script passes new flags to must be the same release."""
    assert Path(tools.await_ci.__file__).resolve().parent == tools.HERE
    assert Path(tools.land_tags.__file__).resolve().parent == tools.HERE
    gate_file = sys.modules[tools.health_gate.__module__].__file__
    assert gate_file and Path(gate_file).resolve().parent == tools.HERE


def test_read_state_is_empty_for_a_missing_or_blank_marker(tmp_path):
    assert tools.read_state(tmp_path, "hold_sha") == ""
    (tmp_path / "hold_sha").write_text("  \n")
    assert tools.read_state(tmp_path, "hold_sha") == ""
    (tmp_path / "hold_sha").write_text("abc\n")
    assert tools.read_state(tmp_path, "hold_sha") == "abc"


def test_the_syslog_tag_is_the_one_the_board_expects(monkeypatch):
    seen = _capture(monkeypatch)
    tools.syslog("event=landing pr=1")
    assert seen["argv"][:3] == ["logger", "-t", "landing-annotation"]


def _fake_fuser_then_ps(monkeypatch, fuser_stdout: str, ps_stdout: str = ""):
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        if argv[0] == "fuser":
            return subprocess.CompletedProcess(argv, 0, fuser_stdout, "")
        return subprocess.CompletedProcess(argv, 0, ps_stdout, "")

    monkeypatch.setattr(tools.subprocess, "run", run)
    return calls


def test_lock_holder_names_the_pid_alongside_etimes_and_command(monkeypatch):
    """bash's printed `lock held by pid $pid (etimes, command): ...` line named the pid
    separately from the etimes+command ps read; this single string is the only value
    callers get, so the pid must be folded in here rather than dropped (#1085 item 3)."""
    _fake_fuser_then_ps(monkeypatch, "12345", "42 ansible-playbook --tags sonarr")
    assert (
        tools.lock_holder()
        == "pid 12345 (etimes, command): 42 ansible-playbook --tags sonarr"
    )


def test_lock_holder_is_empty_when_nobody_holds_the_lock(monkeypatch):
    _fake_fuser_then_ps(monkeypatch, "")
    assert tools.lock_holder() == ""
