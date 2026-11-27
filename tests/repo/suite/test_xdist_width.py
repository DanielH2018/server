"""`xdist_width` sizes `-n auto` to the run's selection, under the session's ceiling.

Run: uv run pytest tests/repo/suite/test_xdist_width.py
"""

import json
import os
import sys
from pathlib import Path
from dataclasses import dataclass

import pytest

import xdist_width
from lib.proc_testing import run

# A literal, not `xdist_width.WEIGHTS_TABLE`, so these tests fail on behaviour rather than a missing
# name.
WEIGHTS_TABLE = "scripts/dev/pytest_shard_runner_weights.json"


def _tree(root: Path, modules: int, weights: dict[str, float] | None = None) -> Path:
    tests = root / "suite" / "tests"
    tests.mkdir(parents=True)
    for n in range(modules):
        (tests / f"test_m{n}.py").write_text("")
    (tests / "_helper.py").write_text("")
    table = root / WEIGHTS_TABLE
    table.parent.mkdir(parents=True)
    table.write_text(json.dumps(weights or {"suite/tests/test_m0.py": 0.1}))
    return tests


@dataclass
class _Params:
    dir: Path


@dataclass
class _Config:
    args: list[str]
    args_source: pytest.Config.ArgsSource
    invocation_params: _Params
    rootpath: Path


def _config(args: list[str], source: pytest.Config.ArgsSource, base: Path, root: Path):
    return _Config(args, source, _Params(base), root)


def test_files_node_ids_and_directories_are_selected_once_each(tmp_path):
    tests = _tree(tmp_path, 30)
    args = ["suite/tests/test_m0.py", "suite/tests/test_m1.py::test_x", "suite/tests"]
    # The two named files are inside the directory; `_helper.py` is not a test module.
    named = xdist_width.selected_modules(args, tmp_path, tmp_path)
    assert named == xdist_width.selected_modules([str(tests)], tmp_path, tmp_path)
    assert named is not None and len(named) == 30


def test_a_path_it_cannot_see_or_the_repo_root_is_left_to_xdist(tmp_path):
    _tree(tmp_path, 3)
    assert xdist_width.selected_modules(["no/such/dir"], tmp_path, tmp_path) is None
    assert xdist_width.selected_modules(["."], tmp_path, tmp_path) is None


@pytest.mark.parametrize(
    ("seconds", "cap", "workers"),
    [
        # The `-python` hook's modules in runner seconds, which ran fastest on 3.
        ([6.28, 1.17, 7.02, 5.48], 16, 3),
        # Every module under the floor: a worker per floor's worth, not per module.
        ([2.5, 2.5, 2.5, 2.5], 16, 3),
        ([8.07, 8.0, 8.0, 8.0], 16, 4),  # a worker per heaviest module's worth
        ([0.27, 0.01, 0.01, 0.01], 16, 1),  # four light files
        ([21.55] + [1.0] * 100, 16, 6),  # 121s, heaviest 21.55s
        ([21.0] + [1.0] * 219, 4, 4),  # a CI shard on a 4-core runner keeps every core
        ([1.0] * 30, 4, 4),  # the session ceiling of 4 still binds
        ([], 16, 1),
    ],
)
def test_workers_follow_the_recorded_seconds_up_to_the_ceiling(seconds, cap, workers):
    assert xdist_width.workers_for(seconds, cap) == workers


def test_unrecorded_modules_are_charged_the_table_mean(tmp_path):
    weights = {"a/test_x.py": 1.0, "a/test_y.py": 3.0}
    modules = [tmp_path / "a" / "test_x.py", tmp_path / "b" / "test_new.py"]
    assert xdist_width.module_seconds(modules, tmp_path, weights) == [1.0, 2.0]


def test_the_session_variable_is_the_ceiling_and_garbage_falls_back_to_cores(
    monkeypatch,
):
    monkeypatch.setenv("PYTEST_XDIST_AUTO_NUM_WORKERS", "3")
    assert xdist_width.ceiling() == 3
    for garbage in ("0", "four", "-2"):
        monkeypatch.setenv("PYTEST_XDIST_AUTO_NUM_WORKERS", garbage)
        assert xdist_width.ceiling() == (os.process_cpu_count() or 1)


def test_a_one_file_run_gets_one_worker_through_the_hook(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTEST_XDIST_AUTO_NUM_WORKERS", "4")
    _tree(tmp_path, 2)
    config = _config(
        ["suite/tests/test_m0.py"], pytest.Config.ArgsSource.ARGS, tmp_path, tmp_path
    )
    assert xdist_width.pytest_xdist_auto_num_workers(config) == 1


def test_a_few_heavy_modules_get_more_than_one_worker_through_the_hook(
    tmp_path, monkeypatch
):
    """Five modules recorded at 3s each ran on one worker under the count rule."""
    monkeypatch.setenv("PYTEST_XDIST_AUTO_NUM_WORKERS", "4")
    _tree(tmp_path, 5, {f"suite/tests/test_m{n}.py": 3.0 for n in range(5)})
    config = _config(["suite/tests"], pytest.Config.ArgsSource.ARGS, tmp_path, tmp_path)
    assert xdist_width.pytest_xdist_auto_num_workers(config) == 4


def test_a_missing_weight_table_is_left_to_xdist(tmp_path):
    _tree(tmp_path, 2)
    (tmp_path / WEIGHTS_TABLE).unlink()
    config = _config(["suite/tests"], pytest.Config.ArgsSource.ARGS, tmp_path, tmp_path)
    assert xdist_width.pytest_xdist_auto_num_workers(config) is None


def test_a_whole_suite_run_from_testpaths_is_left_to_xdist(tmp_path):
    """The red half: a run that named no path must keep xdist's full width."""
    _tree(tmp_path, 2)
    config = _config(
        ["suite/tests"], pytest.Config.ArgsSource.TESTPATHS, tmp_path, tmp_path
    )
    assert xdist_width.pytest_xdist_auto_num_workers(config) is None


@pytest.mark.parametrize(
    ("given", "started"),
    [("3", "created: 2/2 workers"), ("1", "created: 1/1 worker\n")],
)
def test_an_explicit_n_above_the_session_ceiling_starts_only_the_ceiling(
    tmp_path, given, started
):
    """`pytest -n 8` under a ceiling of 4 ran 8 workers beside k3s on daniel-box."""
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    (tmp_path / "test_one.py").write_text("def test_one():\n    pass\n")
    done = run(
        [sys.executable, "-m", "pytest", "-n", given, "-p", "xdist_width"]
        + ["-p", "no:cacheprovider", "-c", str(tmp_path / "pytest.ini"), str(tmp_path)],
        cwd=tmp_path,
        env={
            **os.environ,
            "PYTEST_XDIST_AUTO_NUM_WORKERS": "2",
            "PYTHONPATH": str(Path(xdist_width.__file__).parent),
        },
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert started in done.stdout, done.stdout
