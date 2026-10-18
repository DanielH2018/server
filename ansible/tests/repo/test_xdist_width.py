"""`xdist_width` sizes `-n auto` to the run's selection, under the session's ceiling.

Run: uv run pytest ansible/tests/repo/test_xdist_width.py
"""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import xdist_width


def _tree(root: Path, modules: int) -> Path:
    tests = root / "suite" / "tests"
    tests.mkdir(parents=True)
    for n in range(modules):
        (tests / f"test_m{n}.py").write_text("")
    (tests / "_helper.py").write_text("")
    return tests


def _config(args, source, base: Path, root: Path):
    return SimpleNamespace(
        args=args,
        args_source=source,
        invocation_params=SimpleNamespace(dir=base),
        rootpath=root,
    )


def test_files_node_ids_and_directories_are_counted_as_modules(tmp_path):
    tests = _tree(tmp_path, 30)
    args = ["suite/tests/test_m0.py", "suite/tests/test_m1.py::test_x", "suite/tests"]
    # Two named files plus the directory's 30; `_helper.py` is not a test module.
    assert xdist_width.selected_modules(args, tmp_path, tmp_path) == 32
    assert xdist_width.selected_modules([str(tests)], tmp_path, tmp_path) == 30


def test_a_path_it_cannot_see_or_the_repo_root_is_left_to_xdist(tmp_path):
    _tree(tmp_path, 3)
    assert xdist_width.selected_modules(["no/such/dir"], tmp_path, tmp_path) is None
    assert xdist_width.selected_modules(["."], tmp_path, tmp_path) is None


@pytest.mark.parametrize(
    ("modules", "cap", "workers"),
    [
        (1, 16, 1),
        (4, 16, 1),  # four files ran fastest on one worker
        (59, 16, 4),  # scripts/lib/tests ran fastest on four
        (114, 16, 8),  # the guard set: 4 and 16 tied, so 8 costs nothing in wall time
        (220, 4, 4),  # a CI shard on a 4-core runner keeps every core
        (114, 4, 4),  # the session ceiling of 4 still binds
        (0, 16, 1),
    ],
)
def test_workers_follow_the_selection_up_to_the_ceiling(modules, cap, workers):
    assert xdist_width.workers_for(modules, cap) == workers


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


def test_a_whole_suite_run_from_testpaths_is_left_to_xdist(tmp_path):
    """The red half: a run that named no path must keep xdist's full width."""
    _tree(tmp_path, 2)
    config = _config(
        ["suite/tests"], pytest.Config.ArgsSource.TESTPATHS, tmp_path, tmp_path
    )
    assert xdist_width.pytest_xdist_auto_num_workers(config) is None
