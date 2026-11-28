"""`case_groups`: two concurrent groups share no scratch file, no debs label and no failure.

Each group's output is one block, and its `[DONE]` line names a chained group's slowest cases.
"""

import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from validate.host_package_cases import Cases
from validate.validate_lib import case_groups
from validate.validate_lib.host_container import PORT, Container, expect
from validate.validate_lib.host_fixtures import run_logged


def _prepared(tmp_path: Path) -> SimpleNamespace:
    debs = tmp_path / "work" / "debs"
    debs.mkdir(parents=True)
    (debs / "real-pi.deb").write_bytes(b"real")
    return SimpleNamespace(
        work=tmp_path / "work",
        debs={"pi-head": debs / "real-pi.deb"},
        versions={"pi-head": "1.0"},
        passed=[],
        failed=[],
        no_verdict=[],
        image="homelab-host-test:24.04",
    )


def test_a_groups_seal_and_failure_stay_out_of_the_other_group(tmp_path):
    cases = _prepared(tmp_path)
    sealing = case_groups.isolate(cases, "by_hand")
    other = case_groups.isolate(cases, "bootstrap_repo_case")

    sealed = sealing.work / "debs" / "sealed-pi.deb"
    sealed.write_bytes(b"sealed")
    sealing.debs["pi-head"] = sealed
    sealing.versions["pi-head"] = "1.0.sealed"
    (sealing.work / "repo" / "InRelease").write_text("by_hand's index")
    (sealing.work / "debs" / "real-pi.deb").write_bytes(b"overwritten")
    sealing.failed.append("by_hand")

    assert {
        "other_pi_head": other.debs["pi-head"].read_bytes(),
        "other_version": other.versions["pi-head"],
        "other_repo": sorted(p.name for p in (other.work / "repo").iterdir()),
        "prepared_deb": (cases.work / "debs" / "real-pi.deb").read_bytes(),
        "failed": (cases.failed, other.failed),
        "image": other.image,
    } == {
        "other_pi_head": b"real",
        "other_version": "1.0",
        "other_repo": [],
        "prepared_deb": b"real",
        "failed": ([], []),
        "image": "homelab-host-test:24.04",
    }


def test_a_group_that_raises_is_recorded_and_its_output_is_one_block(tmp_path, capsys):
    cases = _prepared(tmp_path)

    def broken(group) -> None:
        print("broken starts", file=case_groups.sys.stderr)
        raise ValueError("no such deb")

    def clean(group) -> None:
        print("clean ran", file=case_groups.sys.stderr)

    case_groups.run_groups(cases, [("broken", broken), ("clean", clean)], jobs=2)

    err = capsys.readouterr().err
    broken_block = err[err.index("broken starts") : err.index("[DONE] broken")]
    assert {
        "failed": cases.failed,
        "traceback_in_block": "ValueError: no such deb" in broken_block,
        "clean_outside_block": "clean ran" not in broken_block,
    } == {
        "failed": ["broken: raised ValueError"],
        "traceback_in_block": True,
        "clean_outside_block": True,
    }


def test_a_childs_stderr_through_run_logged_lands_in_its_groups_block(tmp_path, capfd):
    cases = _prepared(tmp_path)
    child = [sys.executable, "-c", "import sys; sys.stderr.write('child line\\n')"]

    def building(group) -> None:
        print("building starts", file=case_groups.sys.stderr)
        run_logged(child, check=True)

    case_groups.run_groups(cases, [("building", building)], jobs=1)

    err = capfd.readouterr().err
    block = err[err.index("building starts") : err.index("[DONE] building")]
    assert "child line" in block


class _Container(Container):
    """Answers `Cases.case`'s checks around a body as a container with a fetched list would."""

    def __init__(self) -> None:
        self.name = "homelab-host-test-upgrade"

    def log_size(self) -> int:
        return 0

    def log_since(self, offset: int) -> str:
        return '"GET /dists/homelab/InRelease HTTP/1.1" 200 -'

    def ok(self, script: str) -> str:
        return f"127.0.0.1:{PORT}_dists_homelab_InRelease"

    def sh(self, script: str, timeout: int = 900) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(script, 0, stdout="", stderr="")


def test_a_case_records_its_seconds_whether_it_passes_or_fails(tmp_path):
    cases = _prepared(tmp_path)
    cases.box = "10.0.0.215"
    group = case_groups.isolate(cases, "chain")

    def fails() -> None:
        time.sleep(0.05)
        expect(False, "it was told to fail")

    Cases.case(group, _Container(), "passes", lambda: time.sleep(0.05))
    Cases.case(group, _Container(), "fails", fails)

    assert {
        "verdicts": (group.passed, group.failed),
        "timed": sorted(name for name, s in group.case_seconds.items() if s >= 0.05),
    } == {"verdicts": (["passes"], ["fails"]), "timed": ["fails", "passes"]}


def test_a_chained_groups_done_line_names_its_three_slowest_cases(tmp_path, capsys):
    cases = _prepared(tmp_path)

    def chain(group) -> None:
        seconds = {"install": 4, "upgrade": 30, "kill": 90, "lock": 2, "remove": 60}
        group.case_seconds.update(seconds)

    def single(group) -> None:
        group.case_seconds["cold install"] = 5

    case_groups.run_groups(cases, [("chain", chain), ("single", single)], jobs=1)

    done = [line for line in capsys.readouterr().err.splitlines() if "[DONE]" in line]
    assert done == [
        "[DONE] chain in 0s; slowest cases: kill 90s, remove 60s, upgrade 30s",
        "[DONE] single in 0s",
    ]
