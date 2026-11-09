"""bootstrap_case's planned items run concurrently and keep their verdicts (#7785).

Each test drives `bootstrap_case.run` with the scratch clones, the kit and the seed stubbed and
`Container` replaced by a fake that answers as the script's real run would, so no Docker is
needed. The fake's script run waits up to WAIT seconds for another item to join it, and records
the most items that were running at one moment.

Run: uv run pytest scripts/validate/tests/test_bootstrap_case_concurrent.py
"""

import os
import re
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest
from validate.validate_lib import bootstrap_case as case

WAIT = 0.5
SLOW = 1.5
DONE = r"^\[DONE\] {label} in (\d+(?:\.\d+)?)s$"
PREFIX = "homelab-host-test-bootstrap-"


class Overlap:
    """Which items' script runs overlapped another's, and the most that ran at one moment."""

    def __init__(self, total: int) -> None:
        self.cond = threading.Condition()
        self.total = total
        self.running: set[str] = set()
        self.accompanied: set[str] = set()
        self.done = 0
        self.peak = 0
        self.started: list[str] = []

    def enter(self, label: str) -> bool:
        """Run `label`; True when another item ran beside it, or every other item had finished."""
        with self.cond:
            if self.running:
                self.accompanied |= self.running | {label}
            self.running.add(label)
            self.started.append(label)
            self.peak = max(self.peak, len(self.running))
            self.cond.notify_all()
            company = self.cond.wait_for(
                lambda: label in self.accompanied or self.done == self.total - 1,
                timeout=WAIT,
            )
            self.running.discard(label)
            self.done += 1
            self.cond.notify_all()
            return bool(company)


def _green_output(item: case.Planned) -> tuple[str, str]:
    """A green run's stdout and apt-get call log for `item`."""
    packages = ("homelab-tools", "homelab-updater", f"homelab-{item.host}")
    debs = " ".join(f"/tmp/w/debs/{p}_0.1+gabc_all.deb" for p in packages)
    calls = f"install -y {debs}\n"
    if item.host == "daniel-pi":
        calls += "update -o x=sources.list.d/azlux.sources\ninstall -y log2ram\n"
    stdout = f"{item.marker}\n" + "".join(
        f"Inst {p} (0.1+gabc local-deb [all])\n" for p in packages
    )
    return stdout, calls


class FakeContainer:
    """A container whose script run answers from the planned item its label names."""

    planned: ClassVar[dict[str, case.Planned]] = {}
    overlap: ClassVar[Overlap] = Overlap(0)
    misbehaving: str | None = None
    slow: str | None = None

    def __init__(
        self, image, name, work, hostname="daniel-pi", mounts=(), network=None
    ):
        self.name, self.hostname, self.mounts = name, hostname, mounts
        self.item = self.planned[name.removeprefix(PREFIX)]
        self.calls = ""

    def boot(self, python: str) -> None:
        pass

    def remove(self) -> None:
        pass

    def ok(self, script: str) -> str:
        show = re.match(r"systemctl show -P (\w+) (\S+)$", script)
        if show:
            prop, unit = show.groups()
            if case.UNITS.get(self.hostname) != unit:
                return "not-found"
            return {"LoadState": "loaded", "UnitFileState": "disabled"}.get(
                prop, "inactive"
            )
        if script.startswith("id -nG"):
            return "ubuntu ssh-users\n"
        if script.startswith("systemctl is-enabled"):
            return "masked\n"
        return ""

    def read(self, path: str) -> str:
        if path == case.APT_CALLS:
            return self.calls
        return "/swapfile none swap sw 0 0\n"

    def sh(self, script: str, timeout: int = 900) -> subprocess.CompletedProcess:
        item = self.item
        if not self.overlap.enter(item.label):
            return subprocess.CompletedProcess(script, 99, "", "ran alone")
        if item.label == self.slow:
            time.sleep(SLOW)
        if item.label == self.misbehaving:
            return subprocess.CompletedProcess(script, 0, "", "")
        if item.refusal is None:
            stdout, self.calls = _green_output(item)
            return subprocess.CompletedProcess(script, 0, stdout, "")
        if item.apt_says is not None:
            self.calls = "install -y /tmp/w/debs/homelab-daniel-box_0.1_all.deb\n"
        return subprocess.CompletedProcess(script, 1, item.apt_says or "", item.refusal)


def _green(tmp_path: Path) -> Path:
    """The green kit directory `plan` reads, holding a stand-in k3s."""
    green = tmp_path / "bootstrap-kit" / "green"
    green.mkdir(parents=True, exist_ok=True)
    (green / "k3s").write_text("k3s")
    return green


def _run(
    monkeypatch,
    tmp_path: Path,
    misbehaving: str | None = None,
    slow: str | None = None,
    prepare_seconds: float = 0.0,
):
    """Run every bootstrap case on fake containers; returns the cases and the overlap.

    `slow` names an item whose script run takes SLOW seconds, and `prepare_seconds` is how
    long stocking the kit takes.
    """
    green = _green(tmp_path)
    gating = case.selected(case.plan(tmp_path, green), k3s_download=False)
    planned = {p.label: p for p in gating}
    overlap = Overlap(len(planned))

    def made(out: Path) -> Path:
        out.mkdir(parents=True, exist_ok=True)
        return out

    monkeypatch.setattr(case, "scratch_clone", made)
    monkeypatch.setattr(case, "red_clone", lambda clone, out: made(out))
    monkeypatch.setattr(case, "fixture_clone", lambda clone, out: made(out))
    monkeypatch.setattr(
        case, "stock_kit", lambda cases, clone, kit: time.sleep(prepare_seconds)
    )
    monkeypatch.setattr(case, "stock_seed", lambda kit, seed: made(seed))
    monkeypatch.setattr(FakeContainer, "planned", planned)
    monkeypatch.setattr(FakeContainer, "overlap", overlap)
    monkeypatch.setattr(FakeContainer, "misbehaving", misbehaving)
    monkeypatch.setattr(FakeContainer, "slow", slow)
    monkeypatch.setattr(case, "Container", FakeContainer)
    cases = SimpleNamespace(
        image="img",
        work=tmp_path,
        python="3.12",
        failed=[],
        tools_cache=None,
        k3s_download=False,
    )
    case.run(cases)
    return cases, planned, overlap


def test_the_planned_items_run_concurrently(monkeypatch, tmp_path):
    _, planned, overlap = _run(monkeypatch, tmp_path)

    assert len(planned) >= 8
    assert overlap.peak >= 2, "every bootstrap item ran alone, one after another"


def test_a_concurrent_run_prints_one_verdict_per_item_and_fails_only_the_bad_one(
    monkeypatch, tmp_path, capsys
):
    cases, planned, _ = _run(monkeypatch, tmp_path, misbehaving="red-nfpm")

    bad = planned["red-nfpm"].name
    err = capsys.readouterr().err
    passed = sorted(re.findall(r"^\[PASS\] [^:]+: (.*)$", err, re.MULTILINE))
    failed = re.findall(r"^\[FAIL\] [^:]+: (.*?): ", err, re.MULTILINE)
    assert cases.failed == [bad]
    assert failed == [bad]
    assert passed == sorted(p.name for p in planned.values() if p.name != bad)


def test_every_item_passed_or_failed_prints_how_long_it_took(
    monkeypatch, tmp_path, capsys
):
    _, planned, _ = _run(monkeypatch, tmp_path, misbehaving="red-nfpm")

    err = capsys.readouterr().err
    timed = {
        label
        for label in planned
        if re.search(DONE.format(label=re.escape(label)), err, re.MULTILINE)
    }
    assert timed == set(planned), err


def test_an_item_s_duration_is_its_own_run_not_the_wait_for_earlier_items(
    monkeypatch, tmp_path, capsys
):
    probe = case.selected(case.plan(tmp_path, _green(tmp_path)), k3s_download=False)
    first, last = probe[0].label, probe[-1].label
    _run(monkeypatch, tmp_path, slow=first)

    err = capsys.readouterr().err
    seconds = {
        label: [
            float(s)
            for s in re.findall(DONE.format(label=re.escape(label)), err, re.MULTILINE)
        ]
        for label in (first, last)
    }
    assert len(seconds[first]) == 1 and seconds[first][0] >= SLOW - 0.5, err
    assert len(seconds[last]) == 1 and seconds[last][0] < SLOW - 0.5, err


def test_the_offline_builds_start_before_the_host_items(monkeypatch, tmp_path):
    monkeypatch.setattr(os, "cpu_count", lambda: 3)
    _, _, overlap = _run(monkeypatch, tmp_path)

    assert set(overlap.started[:3]) == {"offline", "red-depends", "fixture-host"}


def test_the_prepare_prints_how_long_it_took(monkeypatch, tmp_path, capsys):
    _run(monkeypatch, tmp_path, prepare_seconds=SLOW)

    err = capsys.readouterr().err
    seconds = [
        float(match.group(1))
        for line in err.splitlines()
        if "prepare" in line
        and (match := re.search(r"\bin (\d+(?:\.\d+)?) ?s\b", line))
    ]
    assert len(seconds) == 1 and seconds[0] >= SLOW - 0.5, err


@pytest.fixture(autouse=True)
def _no_docker(monkeypatch):
    """Refuse any real docker call, so a fake that missed a seam fails rather than boots."""

    def refuse(argv, **kwargs):
        raise AssertionError(f"a test ran a real command: {argv}")

    monkeypatch.setattr(subprocess, "run", refuse)
