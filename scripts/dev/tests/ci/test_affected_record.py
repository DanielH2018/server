"""`affected_lib/record.py`: one traced module run reduced to a row, and the budgeted refresh.

The test that runs a real module under `strace` is in `test_affected_record_traced.py`, because
the recorder cannot trace a module that starts a tracer of its own.

Run: uv run pytest scripts/dev/tests/ci/test_affected_record.py
"""

import pytest
from dev.affected_lib import record, table, trace, traced_pytest

from _helpers import REPO

TRACE_TEST = "scripts/dev/tests/ci/test_affected_trace.py"
TABLE_TEST = "scripts/dev/tests/ci/test_affected_table.py"
RECORD_TEST = "scripts/dev/tests/ci/test_affected_record.py"
EMPTY = table.Row(frozenset(), frozenset(), frozenset(), False)


def test_a_row_keeps_what_the_run_touched_in_the_tracked_tree(tmp_path):
    for directory in ("docs", "lib", "scripts", ".venv"):
        (tmp_path / directory).mkdir()
    root = str(tmp_path)
    touched = trace.Touched(
        reads={
            f"{root}/docs/a.md",
            f"{root}/docs",
            f"{root}/.venv/site.py",
            "/etc/hosts",
        },
        missing={
            f"{root}/docs/new.md",
            f"{root}/lib/sub/deeper/y.py",
            f"{root}/.venv/absent.py",
            f"{root}/lib/__pycache__/x.cpython-315.pyc",
            f"{root}/lib/x.abi3.so",
        },
        listed={
            1: {f"{root}/docs", f"{root}/lib", f"{root}/scripts"},
            2: {f"{root}/lib"},
        },
        root_pid=1,
    )

    row = record.reduce(
        touched,
        harness_listed={f"{root}/lib", f"{root}/scripts"},
        repo=tmp_path,
        tracked=frozenset({"docs/a.md", "docs/b.md", "lib/x.py", "scripts/s.py"}),
        git_index="/git/index",
    )

    assert row == table.Row(
        files=frozenset({"docs/a.md"}),
        # The child's own listing of `lib` stays; the launcher's import-system one does not.
        dirs=frozenset({"docs", "lib"}),
        missing=frozenset({"docs/new.md", "lib/sub/deeper/y.py"}),
        tree=False,
    )


@pytest.mark.parametrize(
    ("escaped", "kept"),
    [(r"caf\303\251.md", {"docs/café.md"}), (r"caf\351.md", set())],
)
def test_a_missing_path_is_kept_only_when_the_table_can_hold_it(
    tmp_path, escaped, kept
):
    (tmp_path / "docs").mkdir()
    log = (
        f'1 openat(AT_FDCWD<{tmp_path}>, "docs/{escaped}", O_RDONLY) = -1 ENOENT '
        "(No such file or directory)\n"
    )

    row = record.reduce(
        trace.parse([log], str(tmp_path)),
        set(),
        tmp_path,
        frozenset({"docs/a.md"}),
        "/git/index",
    )

    assert row.missing == kept
    row_table = table.Table()
    row_table.put(TRACE_TEST, row, "0" * 40)
    out = tmp_path / "table.json"
    table.dump(row_table, out)
    # Python's json accepts a lone surrogate escape; prek's `check json` refuses it.
    assert "\\udc" not in out.read_text()


def test_a_path_under_a_tracked_file_is_recorded_as_that_file(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/b2_drain.py").touch()
    tracked = frozenset({TRACE_TEST, "scripts/b2_drain.py"})
    touched = trace.Touched(
        missing={
            f"{tmp_path}/scripts/b2_drain.py/conda-meta/history",
            f"{tmp_path}/scripts/b2_drain.py/pyvenv.cfg",
        }
    )

    row = record.reduce(touched, set(), tmp_path, tracked, "/git/index")

    assert row.missing == {"scripts/b2_drain.py"}
    rows = table.Table()
    rows.put(TRACE_TEST, row, "0" * 40)
    rows.put(
        TABLE_TEST,
        table.Row(frozenset({"scripts/b2_drain.py"}), frozenset(), frozenset(), False),
        "1" * 40,
    )
    blobs = {
        TRACE_TEST: "0" * 40,
        TABLE_TEST: "1" * 40,
        "scripts/b2_drain.py/x.py": "2" * 40,
    }
    changes = [("D", "scripts/b2_drain.py"), ("A", "scripts/b2_drain.py/x.py")]
    selection = table.select(rows, changes, [TRACE_TEST, TABLE_TEST], blobs)
    assert selection.modules == [TABLE_TEST, TRACE_TEST] and not selection.full, (
        selection.reason
    )


def test_a_run_that_read_gits_index_depends_on_the_whole_tree(tmp_path):
    touched = trace.Touched(reads={"/git/index"}, root_pid=1)

    assert record.reduce(touched, set(), tmp_path, frozenset(), "/git/index").tree


@pytest.mark.parametrize(
    ("filename", "harness"),
    [
        ("<frozen importlib._bootstrap_external>", True),
        ("/venv/lib/python3.15/site-packages/_pytest/pathlib.py", True),
        ("/usr/lib/python3.15/importlib/metadata/__init__.py", True),
        ("/usr/lib/python3.15/pathlib/_local.py", False),
        ("/repo/tests/repo/test_walks_the_tree.py", False),
    ],
)
def test_only_the_import_system_and_the_collector_list_as_the_harness(
    filename, harness
):
    assert traced_pytest.is_harness_frame(filename) is harness


def test_a_directory_a_test_also_listed_is_not_the_harnesss_alone():
    log = "H\t/repo/tests\nH\t/repo/scripts/lib\nT\t/repo/tests\n"

    assert traced_pytest.harness_only(log) == {"/repo/scripts/lib"}


def test_a_refresh_starts_no_module_after_its_budget():
    rows, elapsed = table.Table(), [0.0]

    def record_one(module, repo, tracked, git_index):
        elapsed[0] += 10
        return EMPTY

    recorded, refused = record.refresh(
        rows,
        [TRACE_TEST, TABLE_TEST, RECORD_TEST],
        REPO,
        budget_seconds=15,
        jobs=1,
        record=record_one,
        clock=lambda: elapsed[0],
    )

    assert recorded == [TRACE_TEST, TABLE_TEST] and not refused
    assert set(rows.rows) == {TRACE_TEST, TABLE_TEST}
    assert rows.current(TRACE_TEST, table.tracked_blobs(REPO))


def test_a_refused_module_loses_the_row_it_had():
    rows = table.Table()
    rows.put(TRACE_TEST, EMPTY, "0" * 40)

    recorded, refused = record.refresh(
        rows, [TRACE_TEST], REPO, record=lambda *_: "pytest exit 1"
    )

    assert not recorded and refused == {TRACE_TEST: "pytest exit 1"}
    assert TRACE_TEST not in rows.rows and TRACE_TEST not in rows.stamps
