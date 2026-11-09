"""The phase timing report names the time a run spends outside every test's timed phases.

Each run is a real pytest subprocess over a scratch tree, with a bare `pytest.ini` so the
repo's own `addopts` stay out. The scratch module sleeps at import, and its conftest sleeps in
`pytest_collection_finish` as `conftest.py`'s prebuild does, so the report has a known
collection and prebuild to attribute.

Run: uv run pytest ansible/tests/repo/test_phase_timing.py
"""

import os
import re
import subprocess
import sys
from pathlib import Path

import phase_timing

TESTS = Path(phase_timing.__file__).parent

_CONFTEST = """
import time

def pytest_collection_finish(session):
    time.sleep(0.6)
"""

_MODULE = """
import time

time.sleep(0.3)

def test_{name}():
    time.sleep(0.1)
"""

_WORKER_LINE = re.compile(
    r"^\s*(gw\d|main)  collected\s+([0-9.]+)s  prebuilt\s+([0-9.]+)s"
    r"  tests\s+([0-9.]+)s  finished\s+([0-9.]+)s",
    re.M,
)


def _run(tmp_path: Path, *args: str) -> str:
    for name in ("a", "b"):
        (tmp_path / f"test_{name}.py").write_text(_MODULE.format(name=name))
    (tmp_path / "conftest.py").write_text(_CONFTEST)
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    env["PYTHONPATH"] = str(TESTS)
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "phase_timing", *args],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout


def test_an_xdist_run_reports_each_workers_collection_prebuild_and_finish(tmp_path):
    out = _run(tmp_path, "-n", "2", "--durations=0")
    workers = {m[0]: [float(x) for x in m[1:]] for m in _WORKER_LINE.findall(out)}
    assert sorted(workers) == ["gw0", "gw1"], out
    for name, (collected, prebuilt, tests, finished) in workers.items():
        assert collected >= 0.3, (
            f"{name} collected at {collected}s, before the import sleep"
        )
        assert prebuilt - collected >= 0.5, f"{name} lost the prebuild sleep: {out}"
        assert tests >= 0.1, out
        assert finished > prebuilt, out
    collection = re.search(r"collect\s+([0-9.]+)s\s+test_a\.py", out)
    assert collection and float(collection[1]) >= 0.3, out


def test_a_serial_run_reports_itself_as_main(tmp_path):
    out = _run(tmp_path, "-p", "no:xdist", "--durations=0")
    assert [m[0] for m in _WORKER_LINE.findall(out)] == ["main"], out


def test_a_run_without_durations_prints_no_report(tmp_path):
    out = _run(tmp_path, "-p", "no:xdist")
    assert "phase timing" not in out, out
