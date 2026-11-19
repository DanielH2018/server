"""`scripts/dev/quiet_checks.py` against the fake GitHub `test_ci_failures.py` uses.

Run: uv run pytest scripts/dev/tests/test_quiet_checks.py
"""

import json
import sys
from datetime import date
from pathlib import Path

import quiet_checks
from lib.proc_testing import fake_bin, path_with
from test_ci_failures import FAKE_GITHUB, _attempt, _pytest_job, _run

TODAY = date(2026, 11, 19)


def _report(tmp_path: Path, monkeypatch, capsys, *runs: dict) -> list[str]:
    """Run the report over its default window against a fake GitHub holding `runs`."""
    fake = f"#!{sys.executable} -IS\n{FAKE_GITHUB.read_text()}"
    stubs = fake_bin(tmp_path / "bin", gh=fake, ghread=fake)
    (stubs / "world.json").write_text(json.dumps(runs))
    monkeypatch.setenv("PATH", path_with(stubs))
    assert quiet_checks.main([], today=TODAY) == 0
    return capsys.readouterr().out.splitlines()


def test_a_master_failure_drops_its_file_and_a_pr_branch_failure_does_not(
    tmp_path, monkeypatch, capsys
):
    """The issue's Verify-by: only a red attempt on master keeps a citing file off the list."""
    cited = [p for p in quiet_checks.citing_checks() if p.startswith("tests/repo/")]
    on_master, on_pr = cited[:2]
    yesterday = "2026-11-18T14:02:04Z"

    def red(run_id: int, branch: str, path: str) -> dict:
        module = Path(path).stem
        job = _pytest_job(run_id + 90_000_000_000, module)
        return _run(run_id, _attempt(job), branch=branch, created=yesterday)

    lines = _report(
        tmp_path,
        monkeypatch,
        capsys,
        red(47000000001, "master", on_master),
        red(47000000002, "fix-guard", on_pr),
    )
    listed = {line.split()[0] for line in lines[1:]}
    assert on_pr in listed
    assert on_master not in listed
    assert lines[0].startswith(
        f"{len(listed)} of {len(quiet_checks.citing_checks())} check files"
    )


def test_only_a_file_citing_an_issue_or_a_date_is_a_candidate(tmp_path):
    checks = tmp_path / "tests" / "repo"
    checks.mkdir(parents=True)
    (checks / "test_issue.py").write_text('"""Guards the #1620 outage."""\n')
    (checks / "test_dated.py").write_text("# The 2026-11-18 audit found it.\n")
    (checks / "test_plain.py").write_text('"""Holds every unit to a level."""\n')
    (checks / "_helper.py").write_text("# #4508\n")
    assert quiet_checks.citing_checks(tmp_path) == {
        "tests/repo/test_dated.py": ["2026-11-18"],
        "tests/repo/test_issue.py": ["#1620"],
    }
