"""land.sh run as a user that hands its landings to a lander unit (`land_lib/handoff.py`).

The fake `systemctl` stands in for the unit: its `start` writes the verdict file the way the
unit's land.sh would, and its `show` answers the exit code the unit recorded. A verdict file
the fake did not write during the start stands for an earlier landing's.

Run: uv run pytest scripts/deploy_tools/tests/test_land_handoff.py
"""

import subprocess
from pathlib import Path

import pytest

import land
from deploy_tools.land_lib import handoff
from deploy_tools.land_lib.options import HANDOFF_ENV, PRIMARY_ENV
from deploy_tools.land_lib.tools import Tools
from lib.exit_codes import LAND_BAD_ARGS

UNIT = "claude-land"
PR = "3633"
INSTANCE = f"{UNIT}@{PR}.service"
LANDED = "VERDICT: settled (PR #3633, tags: none)"


class FakeSystemctl:
    """`systemctl start` writes `line` (None writes nothing); `show` answers `status`."""

    def __init__(self, verdict: Path, line=None, status=None, stderr="", raises=None):
        self.verdict = verdict
        self.line = line
        self.status = status or {"Result": "success", "ExecMainStatus": "0"}
        self.stderr = stderr
        self.raises = raises
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *args: str, timeout: float) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        if args[0] == "start":
            if self.raises:
                raise self.raises
            if self.line is not None:
                self.verdict.parent.mkdir(parents=True, exist_ok=True)
                self.verdict.write_text(self.line + "\n")
            return subprocess.CompletedProcess(
                args, 1 if self.stderr else 0, "", self.stderr
            )
        shown = "\n".join(f"{key}={value}" for key, value in self.status.items())
        return subprocess.CompletedProcess(args, 0, shown + "\n", "")


def _hand_off(tmp_path, **fake):
    systemctl = FakeSystemctl(tmp_path / UNIT / f"{PR}.verdict", **fake)
    rc = handoff.land_through_unit(PR, UNIT, systemctl, state_root=tmp_path)
    return rc, systemctl


def test_a_landing_the_unit_settles_is_reported_as_this_landings(tmp_path, capsys):
    rc, systemctl = _hand_off(tmp_path, line=LANDED)
    assert rc == 0
    assert systemctl.calls[0] == ("start", INSTANCE)
    assert LANDED in capsys.readouterr().out


def test_the_exit_code_is_the_one_the_unit_recorded(tmp_path):
    """A VERDICT line does not carry the code: 75 (gave up, re-run) reads like any verdict."""
    rc, _ = _hand_off(
        tmp_path,
        line="VERDICT: tick-timeout (PR #3633)",
        status={"Result": "exit-code", "ExecMainStatus": "75"},
    )
    assert rc == 75


def test_a_landing_that_stopped_without_a_verdict_reports_why(tmp_path, capsys):
    line = (
        "STOPPED: rc=1 PR #3633 — refused by the landing policy: its branch is outside"
    )
    rc, _ = _hand_off(
        tmp_path, line=line, status={"Result": "exit-code", "ExecMainStatus": "1"}
    )
    assert rc == 1
    assert "land: PR #3633 — refused by the landing policy" in capsys.readouterr().err


def test_an_earlier_landings_verdict_is_never_reported_as_this_ones(tmp_path, capsys):
    old = tmp_path / UNIT / f"{PR}.verdict"
    old.parent.mkdir()
    old.write_text(LANDED + "\n")
    rc, _ = _hand_off(
        tmp_path,
        stderr="Job for claude-land@3633.service failed.",
        status={"Result": "exit-code", "ExecMainStatus": "1"},
    )
    out, err = capsys.readouterr()
    assert rc == 1
    assert LANDED not in out
    assert "ended without a verdict (Job for claude-land@3633.service failed.)" in err


@pytest.mark.parametrize(
    ("fake", "reason"),
    [
        (
            {"stderr": "Failed to start claude-land@3633.service: Access denied"},
            "Access denied",
        ),
        (
            {
                "line": "PENDING",
                "status": {"Result": "timeout", "ExecMainStatus": "15"},
            },
            "Result=timeout",
        ),
    ],
    ids=["start refused", "unit killed mid-landing"],
)
def test_a_unit_that_ends_without_a_verdict_names_its_journal(
    tmp_path, capsys, fake, reason
):
    rc, _ = _hand_off(tmp_path, **fake)
    err = capsys.readouterr().err
    assert rc == 1
    assert reason in err and f"journalctl -u {INSTANCE}" in err


def test_a_code_land_sh_never_exits_with_is_reported_as_1(tmp_path):
    rc, _ = _hand_off(
        tmp_path, line=LANDED, status={"Result": "signal", "ExecMainStatus": "143"}
    )
    assert rc == 1


def test_a_start_that_outlasts_the_wait_asks_for_a_re_run(tmp_path):
    rc, _ = _hand_off(tmp_path, raises=subprocess.TimeoutExpired("systemctl", 1))
    assert rc == 75


@pytest.mark.parametrize(
    "flags",
    [["--tags", "sonarr"], ["--since", "abc123"], ["--subject", "Other"]],
    ids=["tags", "since", "subject"],
)
def test_a_flag_the_unit_cannot_honour_is_refused_before_anything_starts(
    monkeypatch, tmp_path, capsys, flags
):
    monkeypatch.setenv(HANDOFF_ENV, UNIT)
    systemctl = FakeSystemctl(tmp_path / "unused")
    rc = land.main(
        ["--pr", PR, "--arm-merge", *flags], tools=Tools(systemctl=systemctl)
    )
    assert rc == LAND_BAD_ARGS
    assert flags[0] in capsys.readouterr().err
    assert not systemctl.calls


def test_a_pr_that_is_not_a_number_is_refused(monkeypatch, tmp_path):
    monkeypatch.setenv(HANDOFF_ENV, UNIT)
    systemctl = FakeSystemctl(tmp_path / "unused")
    assert land.main(["--pr", "0"], tools=Tools(systemctl=systemctl)) == LAND_BAD_ARGS
    assert not systemctl.calls


def test_land_sh_hands_off_rather_than_landing(monkeypatch, tmp_path):
    """The pipeline's first boundary is `gh`; a handoff must start the unit and call nothing else."""
    monkeypatch.setenv(HANDOFF_ENV, UNIT)
    monkeypatch.setenv(PRIMARY_ENV, str(tmp_path))
    systemctl = FakeSystemctl(tmp_path / "unused", stderr="Access denied")

    def no_gh(*args, **kwargs):
        raise AssertionError(f"the handoff called gh: {args}")

    tools = Tools(systemctl=systemctl, gh_json=no_gh, gh=no_gh)
    assert land.main(["--pr", PR, "--arm-merge", "--await-merge"], tools=tools) == 1
    assert systemctl.calls[0] == ("start", INSTANCE)
