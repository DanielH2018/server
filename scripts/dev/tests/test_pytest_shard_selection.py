"""`pytest_shard.py --changed-since`: a pull request's legs carry only what its diff selects.

The selection rule has its own module, `test_affected_table.py`. This one covers what the
shard script adds: reading the diff from git, how many legs a selection needs, and what a leg
tells the workflow.

Run: uv run pytest scripts/dev/tests/test_pytest_shard_selection.py
"""

import ast
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

from dev import affected_tests
import pytest
from dev import pytest_shard
from dev.affected_lib import table

from lib.git_testing import commit, init_repo
from lib.repo_paths import REPO

EMPTY = table.Row(frozenset(), frozenset(), frozenset(), False)


def _outputs(path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in path.read_text().splitlines())


def test_the_diff_from_a_base_selects_its_readers(tmp_path):
    repo = init_repo(tmp_path / "repo")
    tree = {
        "tests/test_reads_x.py": "",
        "tests/test_reads_y.py": "",
        "lib/x.py": "X = 1\n",
        "lib/y.py": "Y = 1\n",
    }
    commit(repo, "base", **tree)
    commit(repo, "change", **{"lib/x.py": "X = 2\n"})
    blobs = table.tracked_blobs(repo)
    rows = table.Table()
    for module, read in (
        ("tests/test_reads_x.py", "lib/x.py"),
        ("tests/test_reads_y.py", "lib/y.py"),
    ):
        rows.put(
            module,
            table.Row(frozenset({read}), frozenset(), frozenset(), False),
            blobs[module],
        )

    selection = pytest_shard.affected("HEAD^1", repo, rows)

    assert not selection.full
    assert selection.modules == ["tests/test_reads_x.py"]


@pytest.mark.parametrize(
    ("selected", "legs"),
    [
        pytest.param(["a"], 1, id="a-light-selection-takes-one-leg"),
        pytest.param(
            ["a", "b", "c"], 2, id="a-leg-carries-no-more-than-a-full-sweeps-share"
        ),
        pytest.param(list("abcdefgh"), 4, id="the-whole-census-takes-every-leg"),
        pytest.param([], 0, id="nothing-selected-takes-none"),
    ],
)
def test_a_selection_takes_as_few_legs_as_keep_each_within_a_full_sweeps_share(
    selected, legs
):
    weights = dict.fromkeys("abcdefgh", 10.0)

    assert pytest_shard.legs_for(selected, list(weights), 4, weights) == legs


def test_a_leg_the_selection_does_not_need_writes_an_empty_list_and_says_so(
    tmp_path, monkeypatch
):
    chosen = pytest_shard.census()[:3]
    outputs = tmp_path / "outputs"
    monkeypatch.setenv("GITHUB_OUTPUT", str(outputs))

    def select(base):
        assert base == "HEAD^1"
        return table.Selection(chosen, False, "3 changed path(s)")

    lists = []
    for shard in (1, 2):
        out = tmp_path / f"shard{shard}.txt"
        argv = [
            "--of",
            "6",
            "--shard",
            str(shard),
            "--changed-since",
            "HEAD^1",
            "--out",
            str(out),
        ]
        assert pytest_shard.main(argv, select=select) == 0
        lists.append(out.read_text().split())

    assert lists == [sorted(chosen), []]
    assert outputs.read_text().splitlines() == [
        "count=3",
        "scope=selected",
        "count=0",
        "scope=selected",
    ]


def test_a_selection_that_is_the_whole_census_runs_as_a_full_sweep_does(
    tmp_path, monkeypatch
):
    outputs, out = tmp_path / "outputs", tmp_path / "shard.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(outputs))
    census = pytest_shard.census()

    def select(base):
        return table.Selection(census, True, "pyproject.toml decides what a run is")

    argv = ["--of", "6", "--shard", "3", "--changed-since", "HEAD^1", "--out", str(out)]
    assert pytest_shard.main(argv, select=select) == 0

    assert out.read_text().split() == pytest_shard.shard_files(3, 6)
    assert _outputs(outputs) == {
        "count": str(len(pytest_shard.shard_files(3, 6))),
        "scope": "full",
    }


def test_a_run_with_no_base_never_asks_for_a_selection(tmp_path, monkeypatch):
    outputs = tmp_path / "outputs"
    monkeypatch.setenv("GITHUB_OUTPUT", str(outputs))

    def select(base):
        raise AssertionError("a push run diffs against nothing")

    argv = ["--of", "6", "--shard", "1", "--out", str(tmp_path / "shard.txt")]
    assert pytest_shard.main(argv, select=select) == 0

    assert _outputs(outputs)["scope"] == "full"


def test_the_plan_starts_exactly_the_legs_the_selection_fills():
    census = pytest_shard.census()
    selection = table.Selection(census[: len(census) // 3], False, "a third")

    plan = pytest_shard.planned_legs(6, selection)

    assert 1 < len(plan) < 6
    assert plan == list(range(1, len(plan) + 1))
    assert all(pytest_shard.leg_files(n, 6, selection) for n in plan)
    assert pytest_shard.leg_files(len(plan) + 1, 6, selection) == []


@pytest.mark.parametrize(
    ("modules", "full", "legs"),
    [
        pytest.param([], False, "[1]", id="no-module-still-starts-one-leg"),
        pytest.param(
            None, True, "[1,2,3,4,5,6]", id="the-whole-census-starts-every-leg"
        ),
    ],
)
def test_the_plan_cli_publishes_the_legs_as_json(
    tmp_path, monkeypatch, capsys, modules, full, legs
):
    outputs = tmp_path / "outputs"
    monkeypatch.setenv("GITHUB_OUTPUT", str(outputs))
    chosen = pytest_shard.census() if modules is None else modules

    def select(base):
        assert base == "HEAD^1"
        return table.Selection(chosen, full, "why")

    argv = ["--plan", "--changed-since", "HEAD^1"]
    assert pytest_shard.main(argv, select=select) == 0

    assert _outputs(outputs) == {"legs": legs}
    assert capsys.readouterr().out == f"legs {legs} of 6: why\n"


def test_a_plan_with_no_base_is_refused(capsys):
    with pytest.raises(SystemExit):
        pytest_shard.main(["--plan"], select=lambda base: None)

    assert "--plan needs --changed-since" in capsys.readouterr().err


def test_the_cron_records_the_modules_with_no_current_row_before_its_slice():
    census = [f"scripts/x/tests/test_{name}.py" for name in "abcdefghijkl"]
    blobs = {module: "a" * 40 for module in census}
    rows = table.Table()
    for module in census[2:]:
        rows.put(module, EMPTY, blobs[module])
    rows.put(census[1], EMPTY, "b" * 40)
    slices = 3
    half_day = 12 * 60 * 60

    visits = [
        affected_tests.due(rows, census, blobs, slices, n * half_day)
        for n in range(slices)
    ]

    for due in visits:
        assert due[:2] == census[:2], (
            "the module with no row and the edited one come first"
        )
    recorded_again = [module for due in visits for module in due[2:]]
    assert sorted(recorded_again) == census[2:], (
        "three runs record every current row once"
    )


@pytest.mark.parametrize(
    ("planned", "refused"),
    [
        pytest.param(lambda plan: plan, False, id="the-plan-it-would-make"),
        pytest.param(lambda plan: plan[:-1], True, id="a-plan-one-leg-short"),
    ],
)
def test_a_leg_whose_selection_fills_more_legs_than_the_plan_started_fails(
    tmp_path, planned, refused
):
    census = pytest_shard.census()
    selection = table.Selection(census[: len(census) // 3], False, "a third")
    plan = planned(pytest_shard.planned_legs(6, selection))
    argv = ["--shard", "1", "--changed-since", "HEAD^1", "--planned", json.dumps(plan)]
    argv += ["--out", str(tmp_path / "shard.txt")]

    if refused:
        with pytest.raises(SystemExit, match=f"but the plan started {len(plan)}"):
            pytest_shard.main(argv, select=lambda base: selection)
    else:
        assert pytest_shard.main(argv, select=lambda base: selection) == 0


def test_the_plan_runs_on_the_standard_library_and_python_3_14(tmp_path):
    """CI's plan step runs on the runner's own `python3`, 3.14, with no venv and no setup.

    `-S` keeps site-packages off the path, so an import outside the standard library fails
    the run. `feature_version` checks grammar only, not the 3.15 standard library's additions.
    """
    probe, loaded = tmp_path / "probe.py", tmp_path / "loaded.txt"
    probe.write_text(
        textwrap.dedent(
            f"""
            import atexit, runpy, sys
            def record():
                files = (getattr(m, "__file__", None) for m in list(sys.modules.values()))
                with open({str(loaded)!r}, "w") as out:
                    out.write("\\n".join(f for f in files if f))
            atexit.register(record)
            sys.argv = ["pytest_shard.py", "--plan", "--changed-since", "HEAD"]
            runpy.run_path("scripts/dev/pytest_shard.py", run_name="__main__")
            """
        )
    )
    done = subprocess.run(
        [sys.executable, "-S", str(probe)],
        cwd=REPO,
        env={"PATH": os.environ["PATH"], "PYTHONPATH": "scripts"},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr

    files = [Path(f) for f in loaded.read_text().splitlines()]
    ours = [f for f in files if f.is_relative_to(REPO) and f.suffix == ".py"]
    assert REPO / "scripts/dev/affected_lib/table.py" in ours
    for module in ours:
        ast.parse(module.read_text(), str(module), feature_version=(3, 14))
