"""`pytest_shard.py --changed-since`: a pull request's legs carry only what its diff selects.

The selection rule has its own module, `test_affected_table.py`. This one covers what the
shard script adds: reading the diff from git, how many legs a selection needs, and what a leg
tells the workflow.

Run: uv run pytest scripts/dev/tests/test_pytest_shard_selection.py
"""

from dev import affected_tests
import pytest
from dev import pytest_shard
from dev.affected_lib import table

from lib.git_testing import commit, init_repo

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


def test_a_plan_with_no_base_is_refused():
    with pytest.raises(SystemExit):
        pytest_shard.main(["--plan"], select=lambda base: None)


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
