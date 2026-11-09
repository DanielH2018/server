"""The container cases' runner: it runs the case groups concurrently and still fails the leg.

Each test drives `host_package_cases.main()` with the twelve case groups, `bootstrap_case` among them, replaced by stubs that
sleep instead of booting a container, so no Docker is needed. The runner must look the groups
up when `main()` runs, the three `Cases` methods on the instance and each `validate_lib` case
module's `run` on that module, and must run them in this process (threads, not
`multiprocessing`), so that a stub set here is the one it calls (#7784).

Run: uv run pytest scripts/validate/tests/test_host_package_cases_concurrent.py
"""

import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from validate import host_package_cases as cases
from validate.validate_lib import (
    bootstrap_age_case,
    bootstrap_case,
    bootstrap_repo_case,
    conffile_case,
    disarm_case,
    engine_case,
    repos_case,
    secrets_case,
    shadow_case,
)

METHODS = ("upgrade_path", "cold_install", "by_hand")
MODULES = {
    "disarm": disarm_case,
    "secrets_case": secrets_case,
    "conffile_case": conffile_case,
    "engine_case": engine_case,
    "shadow_case": shadow_case,
    "repos_case": repos_case,
    "bootstrap_case": bootstrap_case,
    "bootstrap_repo_case": bootstrap_repo_case,
    "bootstrap_age_case": bootstrap_age_case,
}
# `bootstrap_case` runs only under `--bootstrap` or `--k3s-download` (#7819).
GROUPS = (frozenset(METHODS) | frozenset(MODULES)) - {"bootstrap_case"}
SLEEP = 0.3


class Runs:
    """When each stubbed group started and ended."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.spans: dict[str, tuple[float, float]] = {}

    def max_overlap(self) -> int:
        """The most groups that were running at one moment."""
        edges = sorted(
            [(start, 1) for start, _ in self.spans.values()]
            + [(end, -1) for _, end in self.spans.values()]
        )
        running = peak = 0
        for _, step in edges:
            running += step
            peak = max(peak, running)
        return peak


def _stub(runs: Runs, name: str, fail: str | None, raise_in: str | None) -> Callable:
    def body(target) -> None:
        start = time.monotonic()
        for line in range(3):
            print(f"stub-output {name} line {line}", file=cases.sys.stderr, flush=True)
            time.sleep(SLEEP / 3)
        with runs.lock:
            runs.spans[name] = (start, time.monotonic())
        if name == raise_in:
            raise RuntimeError(f"{name} broke outside a case")
        if name == fail:
            target.failed.append(name)
            print(f"[FAIL] stub: {name}: it was told to fail", file=cases.sys.stderr)

    return body


def _run_main(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fail: str | None = None,
    raise_in: str | None = None,
    flags: tuple[str, ...] = (),
) -> tuple[Runs, int | BaseException]:
    runs = Runs()
    monkeypatch.setattr(cases.Cases, "_prepare", lambda self, render, debs: None)
    monkeypatch.setattr(cases, "box_address", lambda render: "10.0.0.215")
    for method in METHODS:
        monkeypatch.setattr(cases.Cases, method, _stub(runs, method, fail, raise_in))
    for name, module in MODULES.items():
        monkeypatch.setattr(module, "run", _stub(runs, name, fail, raise_in))
    argv = ["--image", "homelab-host-test:24.04", "--python", "3.12"]
    argv += ["--render", str(tmp_path / "render"), "--debs", str(tmp_path / "debs")]
    argv += ["--work", str(tmp_path / "work"), *flags]
    try:
        status: int | BaseException = cases.main(argv)
    except Exception as exc:
        status = exc
    return runs, status


def test_a_failing_case_fails_the_concurrent_leg(tmp_path, monkeypatch):
    runs, status = _run_main(tmp_path, monkeypatch, fail="cold_install")

    assert {
        "status": status,
        "ran": set(runs.spans),
        "concurrent": runs.max_overlap() >= 2,
    } == {"status": 1, "ran": GROUPS, "concurrent": True}


def test_a_group_that_raises_fails_the_leg_and_the_rest_still_run(
    tmp_path, monkeypatch
):
    runs, status = _run_main(tmp_path, monkeypatch, raise_in="engine_case")

    assert {
        "status": status,
        "ran": set(runs.spans),
        "concurrent": runs.max_overlap() >= 2,
    } == {"status": 1, "ran": GROUPS, "concurrent": True}


def test_each_groups_output_stays_together_in_the_log(tmp_path, monkeypatch, capfd):
    runs, status = _run_main(tmp_path, monkeypatch)

    marked = [
        line.split()[1]
        for line in capfd.readouterr().err.splitlines()
        if line.startswith("stub-output ")
    ]
    blocks = [name for i, name in enumerate(marked) if i == 0 or marked[i - 1] != name]
    assert {
        "status": status,
        "lines": len(marked),
        "interleaved": sorted({name for name in blocks if blocks.count(name) > 1}),
        "concurrent": runs.max_overlap() >= 2,
    } == {"status": 0, "lines": 3 * len(GROUPS), "interleaved": [], "concurrent": True}


def test_the_bootstrap_flag_runs_only_the_bootstrap_group(tmp_path, monkeypatch):
    runs, status = _run_main(tmp_path, monkeypatch, flags=("--bootstrap",))

    assert {"status": status, "ran": set(runs.spans)} == {
        "status": 0,
        "ran": {"bootstrap_case"},
    }


def test_the_k3s_download_flag_runs_only_the_bootstrap_group(tmp_path, monkeypatch):
    runs, status = _run_main(tmp_path, monkeypatch, flags=("--k3s-download",))

    assert {"status": status, "ran": set(runs.spans)} == {
        "status": 0,
        "ran": {"bootstrap_case"},
    }
