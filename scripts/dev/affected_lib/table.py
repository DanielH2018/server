"""The table of what each test module depends on, and which modules a diff can affect.

`affected_tests.py record` writes the table and `pytest_shard.py --changed-since` reads it. A
row says which tracked files a module's own run resolved, which directories it listed, which
paths it asked for and did not find, and whether it read git's index. `record.py` has how a
row is measured.

## How a change selects a module

- A modified or deleted file selects each module whose row holds it.
- An added or deleted file selects each module that listed the nearest directory the path
  sits in, and each module that read git's index. When the change creates or empties
  directories, each of them counts up to the first one both trees share.
- An added file selects each module that asked for that path, or for a directory above it,
  and did not find it.
- A module with no row is always selected, and so is a module whose file changed after its
  row was recorded: editing a test is the commonest way it gains a dependency.

## When the table cannot answer

`select` answers with the whole census, and says why, when a change touches something that
decides what a run is (`FULL_SWEEP_PATHS`, a `conftest.py`), and when a modified or deleted
file is in no row at all. No row holding a file means either that no test reads it or that
the rows which would are older than the file, and the table cannot tell the two apart.

## What a row can miss

DECIDED: a row is compared with the pull request's own diff, not with every change since the
row was recorded. Six commits a day touch a file
every module reads, so a rule that distrusted a row after any such change would select the
whole suite on almost every run. The cost is that a module can gain a dependency on master
after its row was recorded, and a pull request that changes the dependency then leaves the
module out. The nightly sweep in `.github/workflows/pytest-sweep.yml` runs every module and
catches it there.

A row also holds only what the recording host ran. A test that skips there and runs on a CI
runner reads files the row lacks, so `record.py` gives a module that skipped a test no row.

## The file

One JSON object. `modules` maps each module to the start of the git blob id its file had when its
row was recorded. `files`, `dirs` and `missing` each hold `[paths, readers]` pairs, where `readers`
indexes the sorted module names: paths read by the same modules share one pair, which is what
keeps about 550,000 path references under a megabyte. `tree` lists the readers of git's
index. Nothing in the file moves with the clock, so recording an unchanged row again writes
the same bytes.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

# Reach `scripts/lib`: a directly-invoked script gets only its own directory on sys.path.


from lib.git import git

TABLE_PATH = Path(__file__).resolve().parents[1] / "affected_tests.json"

# Paths that decide what a run is, so a change to one runs every module. A trailing `/` covers
# everything below a directory.
FULL_SWEEP_PATHS = (
    "pyproject.toml",
    "uv.lock",
    ".github/workflows/ci.yml",
    ".github/actions/",
    "scripts/dev/affected_tests.json",
    "scripts/dev/affected_tests.py",
    "scripts/dev/affected_lib/",
    "scripts/dev/pytest_shard.py",
)
_KINDS = ("files", "dirs", "missing")
_STAMP_LENGTH = 12


@dataclass(frozen=True)
class Row:
    """What one test module depends on, as repo-relative posix paths.

    Attributes:
        files: Tracked files the module's run resolved.
        dirs: Tracked directories whose entries it read; the repository root is `""`.
        missing: Paths it asked for that did not exist.
        tree: Whether it read git's index, which lists every tracked path.
    """

    files: frozenset[str]
    dirs: frozenset[str]
    missing: frozenset[str]
    tree: bool


def parent_dirs(paths) -> set[str]:
    """Every directory that holds one of `paths`, the repository root included as `""`."""
    found = {""}
    for path in paths:
        while "/" in path:
            path = path.rsplit("/", 1)[0]
            if path in found:
                break
            found.add(path)
    return found


def tracked_blobs(repo: Path) -> dict[str, str]:
    """Each tracked path of the checkout at `repo`, with the blob id git's index holds for it."""
    listed = git("ls-files", "-s", "-z", cwd=repo).stdout
    return {
        path: meta.split()[1]
        for meta, _, path in (
            entry.partition("\t") for entry in listed.split("\0") if entry
        )
    }


@dataclass
class Table:
    """Each recorded module's row, and the module file it was recorded from.

    Attributes:
        rows: The row of each module that has one.
        stamps: The blob id each of those modules' files had when its row was recorded, cut
            to `_STAMP_LENGTH` characters.
    """

    rows: dict[str, Row] = field(default_factory=dict)
    stamps: dict[str, str] = field(default_factory=dict)

    def current(self, module: str, blobs: dict[str, str]) -> bool:
        """Whether `module` has a row recorded from the file `blobs` holds for it."""
        return (
            module in self.rows
            and self.stamps[module] == blobs.get(module, "")[:_STAMP_LENGTH]
        )

    def put(self, module: str, row: Row, blob: str) -> None:
        """Store `row` as recorded from the module file whose blob id is `blob`."""
        self.rows[module], self.stamps[module] = row, blob[:_STAMP_LENGTH]

    def drop(self, module: str) -> None:
        self.rows.pop(module, None)
        self.stamps.pop(module, None)


@dataclass(frozen=True)
class Selection:
    """The modules a diff can affect.

    Attributes:
        modules: The modules to run, sorted.
        full: Whether `modules` is the whole census because the table could not answer.
        reason: One line saying what decided it.
    """

    modules: list[str]
    full: bool
    reason: str


def load(path: Path = TABLE_PATH) -> Table:
    """Read the table at `path`; a missing file is an empty table."""
    if not path.exists():
        return Table()
    data = json.loads(path.read_text())
    names = sorted(data["modules"])
    sets: dict[str, dict[str, set[str]]] = {
        kind: {n: set() for n in names} for kind in _KINDS
    }
    for kind in _KINDS:
        for paths, readers in data[kind]:
            for reader in readers:
                sets[kind][names[reader]].update(paths)
    tree = {names[reader] for reader in data["tree"]}
    rows = {
        name: Row(
            files=frozenset(sets["files"][name]),
            dirs=frozenset(sets["dirs"][name]),
            missing=frozenset(sets["missing"][name]),
            tree=name in tree,
        )
        for name in names
    }
    return Table(rows=rows, stamps=dict(data["modules"]))


def _pairs(rows: dict[str, Row], names: list[str], kind: str) -> list[list]:
    readers: dict[str, list[int]] = {}
    for index, name in enumerate(names):
        for path in getattr(rows[name], kind):
            readers.setdefault(path, []).append(index)
    shared: dict[tuple[int, ...], list[str]] = {}
    for path, who in readers.items():
        shared.setdefault(tuple(who), []).append(path)
    return sorted([sorted(paths), list(who)] for who, paths in shared.items())


def dump(table: Table, path: Path = TABLE_PATH) -> None:
    """Write `table` to `path`, one pair per line so a refresh diffs by line."""
    names = sorted(table.rows)

    def lines(items) -> str:
        body = ",\n".join(json.dumps(item, separators=(",", ":")) for item in items)
        return f"[\n{body}\n]" if items else "[]"

    parts = [
        '"modules": '
        + json.dumps(
            {n: table.stamps[n] for n in names}, indent=0, separators=(",", ": ")
        )
    ]
    parts += [f'"{kind}": {lines(_pairs(table.rows, names, kind))}' for kind in _KINDS]
    tree = [i for i, n in enumerate(names) if table.rows[n].tree]
    parts.append(f'"tree": {json.dumps(tree, separators=(",", ":"))}')
    path.write_text("{\n" + ",\n".join(parts) + "\n}\n")


def _forces_full_sweep(path: str) -> bool:
    return path.rsplit("/", 1)[-1] == "conftest.py" or any(
        path.startswith(entry) if entry.endswith("/") else path == entry
        for entry in FULL_SWEEP_PATHS
    )


def select(
    table: Table,
    changes: list[tuple[str, str]],
    census: list[str],
    blobs: dict[str, str],
) -> Selection:
    """Pick the modules of `census` that `changes` can affect.

    Args:
        table: The recorded rows.
        changes: The diff as `(status, path)` pairs, where the status is git's letter: `A`
            for an added path, `D` for a deleted one, anything else for a modified one. A
            rename arrives as its `D` and `A` halves.
        census: Every test module in the tree the diff leads to.
        blobs: Every tracked path in that tree, with its blob id.
    """

    def whole(reason: str) -> Selection:
        return Selection(sorted(census), True, reason)

    if not table.rows:
        return whole("the table has no rows")
    for _, path in changes:
        if _forces_full_sweep(path):
            return whole(f"{path} decides what a run is")

    modules = set(census)
    added = {path for status, path in changes if status == "A"}
    deleted = {path for status, path in changes if status == "D"}
    covered = set().union(*(row.files for row in table.rows.values()))
    for status, path in changes:
        if status != "A" and path not in covered and path not in modules:
            return whole(f"{path} is in no module's row")

    head_dirs = parent_dirs(blobs)
    base_dirs = parent_dirs((blobs.keys() - added) | deleted)
    membership = set()
    for path in added | deleted:
        while "/" in path:
            path = path.rsplit("/", 1)[0]
            membership.add(path)
            if path in head_dirs and path in base_dirs:
                break
        else:
            membership.add("")
    changed = {path for _, path in changes}
    created = added | {
        path.rsplit("/", depth)[0]
        for path in added
        for depth in range(1, path.count("/") + 1)
    }

    selected = {module for module in modules if not table.current(module, blobs)}
    unrecorded = len(selected)
    for module in modules - selected:
        row = table.rows[module]
        if (
            not changed.isdisjoint(row.files)
            or (membership and (row.tree or not membership.isdisjoint(row.dirs)))
            or not created.isdisjoint(row.missing)
        ):
            selected.add(module)
    return Selection(
        sorted(selected),
        False,
        f"{len(changes)} changed path(s) select {len(selected)} of {len(modules)} modules, "
        f"{unrecorded} of them for having no current row",
    )


def select_since(table: Table, base: str, census: list[str], repo: Path) -> Selection:
    """`select` over the diff from `base` to HEAD in the checkout at `repo`."""
    fields = git(
        "diff", "--name-status", "--no-renames", "-z", base, "HEAD", cwd=repo
    ).stdout.split("\0")
    changes = list(zip(fields[0::2], fields[1::2], strict=False))
    return select(table, changes, census, tracked_blobs(repo))
