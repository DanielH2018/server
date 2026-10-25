"""`xdist_width` sizes `-n auto` to the run's selection, under the session's ceiling.

Run: uv run pytest ansible/tests/repo/test_xdist_width.py
"""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import xdist_width


def _tree(root: Path, modules: int, weights: dict[str, float] | None = None) -> Path:
    tests = root / "suite" / "tests"
    tests.mkdir(parents=True)
    for n in range(modules):
        (tests / f"test_m{n}.py").write_text("")
    (tests / "_helper.py").write_text("")
    table = root / xdist_width.WEIGHTS_TABLE
    table.parent.mkdir(parents=True)
    table.write_text(json.dumps(weights or {"suite/tests/test_m0.py": 0.1}))
    return tests


def _config(args, source, base: Path, root: Path):
    return SimpleNamespace(
        args=args,
        args_source=source,
        invocation_params=SimpleNamespace(dir=base),
        rootpath=root,
    )


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
        # The rows of the 2026-10-25 table: recorded seconds, heaviest module included.
        (
            [1.88, 0.01, 1.59, 2.22, 0.0],
            16,
            3,
        ),  # the `-python` hook's five ran fastest on 3
        (
            [8.07, 8.0, 8.0, 8.0],
            16,
            4,
        ),  # scripts/lib/tests: 32.1s, heaviest 8.07s, on 4
        ([0.27, 0.01, 0.01, 0.01], 16, 1),  # four light files
        ([21.55] + [1.0] * 100, 16, 6),  # the guard set: 121s, heaviest 21.55s
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
    """#5967: five modules recorded at 3s each ran on one worker under the count rule."""
    monkeypatch.setenv("PYTEST_XDIST_AUTO_NUM_WORKERS", "4")
    _tree(tmp_path, 5, {f"suite/tests/test_m{n}.py": 3.0 for n in range(5)})
    config = _config(["suite/tests"], pytest.Config.ArgsSource.ARGS, tmp_path, tmp_path)
    assert xdist_width.pytest_xdist_auto_num_workers(config) == 4


def test_a_missing_weight_table_is_left_to_xdist(tmp_path):
    _tree(tmp_path, 2)
    (tmp_path / xdist_width.WEIGHTS_TABLE).unlink()
    config = _config(["suite/tests"], pytest.Config.ArgsSource.ARGS, tmp_path, tmp_path)
    assert xdist_width.pytest_xdist_auto_num_workers(config) is None


def test_a_whole_suite_run_from_testpaths_is_left_to_xdist(tmp_path):
    """The red half: a run that named no path must keep xdist's full width."""
    _tree(tmp_path, 2)
    config = _config(
        ["suite/tests"], pytest.Config.ArgsSource.TESTPATHS, tmp_path, tmp_path
    )
    assert xdist_width.pytest_xdist_auto_num_workers(config) is None
