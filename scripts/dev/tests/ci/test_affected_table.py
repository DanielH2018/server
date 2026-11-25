"""`affected_lib/table.py`: which test modules a diff selects, and the file the rows live in.

Every case builds its own small table, so nothing here reads the committed one except the
last test, which checks the paths the rule names against the tree.

Run: uv run pytest scripts/dev/tests/ci/test_affected_table.py
"""

import pytest
from dev.affected_lib import table

from _helpers import REPO

READER = "tests/repo/test_reader.py"
LISTER = "tests/repo/test_lister.py"
ASKER = "tests/repo/test_asker.py"
INDEXER = "tests/repo/test_indexer.py"
NARROW = "tests/repo/test_narrow.py"
MODULES = [READER, LISTER, ASKER, INDEXER, NARROW]
TREE = [
    *MODULES,
    "docs/a.md",
    "docs/guides/b.md",
    "k8s/roles/x/defaults/main.yml",
    "lib/n.py",
]


def _row(files=(), dirs=(), missing=(), tree=False) -> table.Row:
    return table.Row(frozenset(files), frozenset(dirs), frozenset(missing), tree)


def _blobs(paths=TREE) -> dict[str, str]:
    return {path: f"{index:040x}" for index, path in enumerate(paths)}


def _table(blobs=None) -> table.Table:
    blobs = _blobs() if blobs is None else blobs
    rows = table.Table()
    rows.put(
        READER, _row(files=[READER, "docs/a.md", "docs/guides/b.md"]), blobs[READER]
    )
    rows.put(LISTER, _row(files=[LISTER], dirs=["docs"]), blobs[LISTER])
    rows.put(
        ASKER,
        _row(files=[ASKER], missing=["k8s/roles/x/templates", "docs/conftest.py"]),
        blobs[ASKER],
    )
    rows.put(INDEXER, _row(files=[INDEXER], tree=True), blobs[INDEXER])
    rows.put(NARROW, _row(files=[NARROW, "lib/n.py"]), blobs[NARROW])
    return rows


def _selected(changes, tree=TREE, modules=MODULES) -> list[str]:
    blobs = _blobs(tree)
    selection = table.select(_table(blobs), changes, modules, blobs)
    assert not selection.full, selection.reason
    return selection.modules


@pytest.mark.parametrize(
    ("changes", "tree", "expected"),
    [
        pytest.param(
            [("M", "docs/a.md")], TREE, [READER], id="modified-file-selects-its-readers"
        ),
        pytest.param(
            [("A", "docs/new.md")],
            [*TREE, "docs/new.md"],
            [INDEXER, LISTER],
            id="added-file-selects-the-directorys-listers-and-the-index-readers",
        ),
        pytest.param(
            [("D", "docs/a.md")],
            [p for p in TREE if p != "docs/a.md"],
            [INDEXER, LISTER, READER],
            id="deleted-file-selects-its-readers-too",
        ),
        pytest.param(
            [("A", "docs/guides/deep/new/c.md")],
            [*TREE, "docs/guides/deep/new/c.md"],
            [INDEXER],
            id="a-new-directory-reaches-no-further-up-than-the-one-it-appears-in",
        ),
        pytest.param(
            [("D", "docs/guides/b.md")],
            [p for p in TREE if p != "docs/guides/b.md"],
            [INDEXER, LISTER, READER],
            id="emptying-a-directory-removes-an-entry-from-its-parent",
        ),
        pytest.param(
            [("A", "k8s/roles/x/templates/deployment.yaml.j2")],
            [*TREE, "k8s/roles/x/templates/deployment.yaml.j2"],
            [ASKER, INDEXER],
            id="added-file-selects-a-module-that-asked-for-its-directory",
        ),
    ],
)
def test_a_change_selects_the_modules_whose_rows_it_touches(changes, tree, expected):
    assert _selected(changes, tree) == expected


def test_a_module_with_no_row_is_always_selected():
    new = "tests/repo/test_new.py"

    assert _selected([("M", "lib/n.py")], [*TREE, new], [*MODULES, new]) == [
        NARROW,
        new,
    ]


def test_a_module_edited_since_its_row_was_recorded_is_selected():
    blobs = _blobs()
    rows = _table(blobs)
    edited = blobs | {LISTER: "f" * 40}

    selection = table.select(rows, [("M", "lib/n.py")], MODULES, edited)

    assert selection.modules == [LISTER, NARROW]


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        pytest.param(
            [("M", "pyproject.toml")], "pyproject.toml decides", id="run-config"
        ),
        pytest.param(
            [("A", "docs/conftest.py")], "docs/conftest.py decides", id="a-conftest"
        ),
        pytest.param(
            [("M", "docs/unread.md")], "in no module's row", id="a-file-no-row-holds"
        ),
    ],
)
def test_the_whole_census_runs_when_the_table_cannot_answer(changes, reason):
    blobs = _blobs([*TREE, "docs/unread.md"])

    selection = table.select(_table(blobs), changes, MODULES, blobs)

    assert selection.full and selection.modules == sorted(MODULES)
    assert reason in selection.reason


def test_an_empty_table_selects_the_whole_census():
    """An added file is in no row by definition, so only the empty-table rule can answer."""
    blobs = _blobs([*TREE, "docs/new.md"])

    selection = table.select(table.Table(), [("A", "docs/new.md")], MODULES, blobs)

    assert selection.full and selection.modules == sorted(MODULES)
    assert "no rows" in selection.reason


def test_a_table_reads_back_as_it_was_written_and_stores_shared_paths_once(tmp_path):
    rows = _table()
    rows.put(LISTER, _row(files=[LISTER, "docs/a.md", "docs/guides/b.md"]), "b" * 40)
    path = tmp_path / "affected.json"

    table.dump(rows, path)
    read = table.load(path)

    assert read.rows == rows.rows and read.stamps == rows.stamps
    assert path.read_text().count('"docs/a.md"') == 1


def test_recording_the_same_rows_again_writes_the_same_bytes(tmp_path):
    first, second = tmp_path / "first.json", tmp_path / "second.json"

    table.dump(_table(), first)
    table.dump(table.load(first), second)

    assert first.read_bytes() == second.read_bytes()


def test_every_full_sweep_path_names_something_in_the_tree():
    """A renamed workflow or script would otherwise drop out of the rule without a failure."""
    tracked = table.tracked_blobs(REPO)
    lost = [
        entry
        for entry in table.FULL_SWEEP_PATHS
        if not any(
            p.startswith(entry) if entry.endswith("/") else p == entry for p in tracked
        )
    ]

    assert not lost
    assert "scripts/dev/pytest_shard.py" in table.FULL_SWEEP_PATHS
