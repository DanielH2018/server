#!/usr/bin/env python3
"""Split the pytest suite into N fixed shards of whole test modules, for CI's matrix.

WHY. The `pytest` job is the pole of every CI run and a landing waits on it twice — once on
the PR, once on the master merge commit that `land.sh` and checkout-sync's CI gate both read.
Sharding across matrix jobs is the remaining lever.

WHOLE MODULES, NEVER PART OF ONE. `--dist loadscope` in `addopts` keeps a module's tests on
one xdist worker so a module-scoped fixture is built once. Splitting a module across shards
would rebuild those fixtures per shard and give the saving straight back.

WHY WEIGHTS, AND NOT A HASH. A stable hash of the module path is the obvious split and it
balances badly here: the suite's cost is concentrated, not flat. Measured against a serial
`--durations=0` run of the whole suite (103.6s over 505 files), projected pole shard at N=4 was
42.7s for a sha256 split and 39.8s for a greedy split weighted by the number of `def test_` in
each file, against 24.9s for a greedy split weighted by measured per-file seconds — an even
split being 25.9s. File byte size fared no better than the hash (36.2s). So the split is
longest-processing-time bin packing over per-file durations: the runner seconds in
`pytest_shard_runner_weights.json`, which `shard_weight_gate.RUNNER_WEIGHTS_PATH` describes.

THE TABLE IS CI'S OWN MEASUREMENT. The docs-refresh cron runs `--refresh-runner` twice a day,
which reads every pytest leg's log from the newest green master runs, and nothing measured on
a workstation is written to it. `shard_weight_gate.refresh_runner_weights` has what a refresh
adds, rewrites and drops.

STALENESS IS GRACEFUL, WHICH IS WHY THE TABLE IS SAFE TO COMMIT. A path the table does not
list is given the median of its directory's rows, and a row whose path no longer
exists is ignored. Both cases cost balance and nothing else: every collected file still lands
in exactly one shard, so a stale table makes CI slower, never wrong.

THE DIRECTORY MEDIAN IS A BAD GUESS FOR A HEAVY FILE, AND THE TABLE CANNOT KNOW WHICH FILES
ARE HEAVY. A module is unlisted from the commit that adds it until a refresh reads it off a
master run, and for that long it is packed like its neighbours. One unlisted 30s module packed
as light skewed four CI shards to 65/99/99/68s against a 43s projection.

IT IS A HIGH GUESS FOR A MODULE THAT NEVER GAINS A ROW. A module whose every test runs under
pytest's 0.005s report floor prints no durations line, so no refresh lists it. On 2026-11-19
that was 174 of 1,459 modules. Their directory medians total about 42 runner seconds that the
legs do not spend, which moved the projected gap between six legs of 368s by under 6s.

ONE CHECK WATCHES THE TABLE, AND IT IS A CI STEP: `--check-durations`. It reads the durations
report CI's own test step already wrote, so it measures each module by running it. That is what
lets it see the shapes nothing static can. A heavy module landing where every recorded
sibling is light is invisible to a neighbour heuristic, and `def test_` count, byte size and
directory mean all fail to separate a heavy module from an ordinary file; the
check annotates it at `RUNNER_HEAVY_SECONDS` (in `shard_weight_gate.py`, with the arms).
A recorded number can also be wrong while present. A recorded 17.31s against 0.09s measured
makes the greedy split place the file first, so one shard carries 17s of phantom weight, and
a module that grew after its weight was recorded measured 45.67s against 0.48s and was packed
as free. The check answers each as a RATIO: `shard_weight_gate.STALE_WEIGHT_RATIO` and
`shard_weight_gate.UNDER_WEIGHT_RATIO`.

A CACHE SHARED ACROSS MODULES IS A PER-WORKER COST, NOT A MODULE'S. Under xdist every worker
builds `_k8s_render`'s render once and charges it to whichever module asks first, so its weight
would vary from run to run. `tests/render_share.py` builds it when collection
finishes, outside every test's timed phases, with the fleet render beside it, and its
`# DECIDED:` has the numbers. `scripts/tests/conftest.py` runs the two census files' shared
pass the same way. `shard_prebuild.py` says which modules trigger which of the three. A
leg builds each of the three only when it holds a module that reads it,
so its cost depends on the shard's membership: the legs holding the census files ran 14-17s
longer than a split on test seconds alone projected. `assign` therefore packs each
shard's `prebuild_seconds` beside its test seconds, and `--summary` prints both.

A PULL REQUEST RUNS ONLY THE MODULES ITS DIFF CAN AFFECT. `--changed-since REF` asks
`affected_lib/table.py` which modules the diff from REF selects and packs those alone, into as
few legs as keep each no heavier than a full sweep's leg. The legs past that count get an
empty list and CI skips their remaining steps. A push to master passes the newest ancestor
whose CI passed as REF. A dispatch passes no REF and runs the whole census, as does a push
with no such ancestor in reach. `affected_lib/table.py` has the rule and what it can miss.

THE CHECK NEVER FAILS A RUN. It prints each complaint as a `::warning::` annotation and exits
zero on every event. A wrong weight costs shard balance and nothing else, at most the module's
own runner seconds on one leg, and the docs-refresh cron repairs it unattended within a day:
`--refresh-runner` reads the table's rows off green master runs. A failed run costs a full rerun of every job
from a runner pool that is already the throughput limit, so failing on a weight spends more CI
time than the weight costs. Each bound also compares one wall-clock reading against a fixed
number, and runner variance spans it: one module measured about 8s and 15.6s on two runs of
the same code. Each narrowing of a failing version answered a run it failed
for no fault of the PR in front of it.

Usage:
    uv run python scripts/dev/pytest_shard.py --of 4 --shard 1        # this shard's files
    uv run python scripts/dev/pytest_shard.py --of 4 --shard 1 --out list.txt
    uv run python scripts/dev/pytest_shard.py --of 4 --shard 1 --changed-since HEAD^1
    uv run python scripts/dev/pytest_shard.py --of 4 --summary        # projected balance
    uv run python scripts/dev/pytest_shard.py --refresh-runner 3      # the table, from CI's logs
    uv run python scripts/dev/pytest_shard.py --leg-spread 42541064791  # one run's leg walls
    uv run python scripts/dev/pytest_shard.py --check-durations ci.log  # annotate, always exit 0
"""

import math
import os
import posixpath
import statistics
import sys
import tomllib
from pathlib import Path, PurePosixPath

# Reach `scripts/lib`: a directly-invoked script gets only its own directory on sys.path,
# and pyproject's `pythonpath` is a pytest setting.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# Reach `render_share`, which decides which test modules read the fleet render.
sys.path.insert(1, str(Path(__file__).resolve().parents[2] / "tests"))

from affected_lib import table as affected_table
from lib.cli_help import ArgumentParser

from lib.git import git
from lib.repo_paths import REPO
from shard_prebuild import (
    CENSUS_FILE_PREFIX,
    CI_WORKERS,
    prebuild_kinds,
    prebuild_seconds,
    prebuild_wall,
)
from shard_weight_gate import (
    RUNNER_HEAVY_SECONDS,
    RUNNER_WEIGHTS_PATH,
    STALE_WEIGHT_RATIO,
    durations_complaints,
    fetch_runner_logs,
    green_pytest_legs,
    leg_spread,
    load_runner_weights,
    refresh_runner_weights,
    write_runner_weights,
)

# pytest's default `python_files`, both forms — the same pair
# `tests/repo/test_testpaths_covers_every_test_file.py` derives its census from, and for
# the same reason: a single hand-kept glob would miss a `foo_test.py` that pytest collects.
_TEST_FILE_GLOBS = ("test_*.py", "*_test.py")

# What a file costs before any of its tests run: pytest imports the module at collection, and
# the durations report attributes that to no test. Timing two N=4 shards
# serially against their recorded totals — a 49-file shard ran 26.0s against 24.5s recorded and
# a 357-file shard 34.2s against the same 24.5s, so the residual is 0.031s and 0.027s per file.
# Without this term the split balances recorded seconds and leaves the file COUNT wildly uneven,
# which is how the 357-file shard became the pole while the model called every shard equal.
_PER_FILE_SECONDS = 0.028


def testpaths() -> list[str]:
    data = tomllib.loads((REPO / "pyproject.toml").read_text())
    paths = data["tool"]["pytest"]["ini_options"]["testpaths"]
    assert paths, "pyproject.toml declares no testpaths"
    return paths


def under_testpaths(rel: str, paths) -> bool:
    """Whether `rel` lies under a `testpaths` entry, where an entry may be a glob.

    `full_match` against `<entry>/**` matches by path component, so a `scripts` entry does not
    cover `scripts_extra/test_x.py`, and `k8s/roles/*/tests` covers every role's suite.
    """
    return any(PurePosixPath(rel).full_match(f"{entry}/**") for entry in paths)


def census(repo: Path = REPO) -> list[str]:
    """Every test file this commit's `testpaths` reach, as repo-relative posix paths.

    DECIDED: `git ls-files`, not `rglob` — the reason
    `test_testpaths_covers_every_test_file.py` gives. This repo grows a full working tree per
    live session under `.claude/worktrees/<name>/`, so an rglob would shard other sessions'
    copies of these same files alongside this commit's. The cost is that a test file you have
    written but not yet `git add`ed is in no shard; CI only ever sees committed files, so this
    bites in a local `--summary` and never on the runner.
    """
    listed = git("ls-files", "-z", cwd=repo).stdout
    paths = testpaths()
    return sorted(
        rel
        for rel in listed.split("\0")
        if rel
        and any(PurePosixPath(rel).match(g) for g in _TEST_FILE_GLOBS)
        and under_testpaths(rel, paths)
    )


def packing_seconds(files, weights: dict[str, float]) -> dict[str, float]:
    """The test seconds each of `files` is packed at.

    A file `weights` lists takes its row. Any other takes the median of the rows in its own
    directory, or of every row when its directory has none.
    """
    by_directory: dict[str, list[float]] = {}
    for module, seconds in weights.items():
        by_directory.setdefault(posixpath.dirname(module), []).append(seconds)
    medians = {d: statistics.median(rows) for d, rows in by_directory.items()}
    fallback = statistics.median(weights.values()) if weights else 1.0
    return {
        f: weights.get(f, medians.get(posixpath.dirname(f), fallback)) for f in files
    }


def _costs(files, weights: dict[str, float]) -> dict[str, float]:
    """What each of `files` costs a leg before any prebuild."""
    return {
        f: seconds + _PER_FILE_SECONDS
        for f, seconds in packing_seconds(files, weights).items()
    }


def assign(
    files, shards: int, weights: dict[str, float] | None = None, repo: Path = REPO
) -> dict[str, int]:
    """Map each file to a shard index in `range(shards)`, greedy longest-processing-time.

    A file's cost on a shard is its `packing_seconds`, `_PER_FILE_SECONDS`, and the prebuild
    seconds it adds to what that shard already pays. The census files go as one unit, since
    they share one census pass, and greedy placement would split them: the second file costs
    less on an empty shard than beside the first. Units go in order of their cost on an empty
    shard, each to the shard it leaves least loaded.

    Deterministic: ties in that order break by path, and a tie between two shards goes to the
    lower index. The same census and the same weights give the same assignment on every
    machine.

    `weights` is the committed runner table when None.
    """
    if shards < 1:
        raise ValueError(f"shards must be >= 1, got {shards}")
    weights = load_runner_weights() if weights is None else weights
    file_cost = _costs(files, weights)
    census_files = tuple(sorted(f for f in files if f.startswith(CENSUS_FILE_PREFIX)))
    units = [(f,) for f in files if f not in census_files]
    units += [census_files] if census_files else []
    cost = {u: sum(file_cost[f] for f in u) for u in units}
    kinds = {u: frozenset().union(*(prebuild_kinds(f, repo) for f in u)) for u in units}
    alone = {u: cost[u] + prebuild_wall(kinds[u]) * CI_WORKERS for u in units}
    load = [0.0] * shards
    paid: list[frozenset[str]] = [frozenset()] * shards
    placed: dict[str, int] = {}
    for unit in sorted(units, key=lambda u: (-alone[u], u)):
        after = [
            load[i]
            + cost[unit]
            + (prebuild_wall(paid[i] | kinds[unit]) - prebuild_wall(paid[i]))
            * CI_WORKERS
            for i in range(shards)
        ]
        target = min(range(shards), key=lambda i: (after[i], i))
        load[target] = after[target]
        paid[target] |= kinds[unit]
        placed |= dict.fromkeys(unit, target)
    return placed


def shard_files(shard: int, shards: int, files=None, weights=None) -> list[str]:
    """The files for 1-based `shard` of `shards`."""
    if not 1 <= shard <= shards:
        raise ValueError(f"shard {shard} is outside 1..{shards}")
    files = census() if files is None else files
    placed = assign(files, shards, weights)
    return sorted(f for f, i in placed.items() if i == shard - 1)


def affected(
    base: str, repo: Path = REPO, rows: affected_table.Table | None = None
) -> affected_table.Selection:
    """The modules the diff from `base` to HEAD can affect, as `rows` reads it.

    `rows` is the committed table when None.
    """
    return affected_table.select_since(
        affected_table.load() if rows is None else rows, base, census(repo), repo
    )


def legs_for(selected, files, shards: int, weights: dict[str, float]) -> int:
    """How many of `shards` legs carry `selected`, so none outweighs a leg of all of `files`."""
    if not selected:
        return 0
    cost = _costs(files, weights)
    per_leg = sum(cost.values()) / shards
    return min(shards, max(1, math.ceil(sum(cost[f] for f in selected) / per_leg)))


def leg_files(
    shard: int, shards: int, selection: affected_table.Selection | None = None
) -> list[str]:
    """What 1-based leg `shard` of `shards` runs.

    With no `selection`, or one that is the whole census, the leg takes its share of the
    census. Otherwise it takes its share of the selected modules, which is empty for a leg the
    selection does not need.
    """
    if selection is None or selection.full:
        return shard_files(shard, shards)
    weights = load_runner_weights()
    legs = legs_for(selection.modules, census(), shards, weights)
    return shard_files(shard, legs, selection.modules, weights) if shard <= legs else []


def _summary(shards: int) -> str:
    files, weights = census(), load_runner_weights()
    placed = assign(files, shards, weights)
    file_cost = _costs(files, weights)
    lines = []
    for i in range(shards):
        members = [f for f, s in placed.items() if s == i]
        cost = sum(file_cost[f] for f in members)
        lines.append(
            f"  shard {i + 1}: {len(members):>4} files, {cost:6.1f} runner seconds projected"
            f" + {prebuild_seconds(members):4.1f} in render prebuilds"
        )
    unknown = sum(1 for f in files if f not in weights)
    lines.append(f"  {len(files)} files, {unknown} packed at their directory's median")
    return "\n".join(lines)


def main(argv=None, select=affected) -> int:
    parser = ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--of", type=int, default=4, help="how many shards in total")
    parser.add_argument("--shard", type=int, help="which 1-based shard to print")
    parser.add_argument(
        "--out", type=Path, help="write the file list here instead of stdout"
    )
    parser.add_argument(
        "--changed-since",
        metavar="REF",
        help=(
            "with --shard, pack only the modules the diff from REF to HEAD can affect; a leg "
            "the selection does not need gets an empty list"
        ),
    )
    parser.add_argument(
        "--summary", action="store_true", help="print the projected balance"
    )
    parser.add_argument(
        "--record-runner",
        nargs="+",
        type=Path,
        metavar="LOG",
        help="rewrite the runner table from CI shard logs, each module at its median",
    )
    parser.add_argument(
        "--refresh-runner",
        type=int,
        metavar="RUNS",
        help=(
            "download every pytest leg's log from the newest RUNS green master runs of CI and "
            "merge their medians into the runner table, rewriting only drifted rows"
        ),
    )
    parser.add_argument(
        "--leg-spread",
        type=int,
        metavar="RUN_ID",
        help="print each green pytest leg's wall in CI run RUN_ID, raw and over its leg_speed",
    )
    parser.add_argument(
        "--check-durations",
        type=Path,
        metavar="LOG",
        help=(
            "read a pytest --durations report and annotate a heavy unweighted module, a "
            "recorded weight far above what its module measured, or one far below it; "
            "always exits 0"
        ),
    )
    args = parser.parse_args(argv)

    if args.check_durations:
        report = args.check_durations.read_text(errors="replace")
        complaints = durations_complaints(report, load_runner_weights())
        if complaints:
            print("\n".join(f"::warning::{c}" for c in complaints))
        else:
            print(
                f"no unweighted or under-recorded module measured {RUNNER_HEAVY_SECONDS:.0f}s "
                f"or more in this shard, and no recorded weight is {STALE_WEIGHT_RATIO:.0f}x "
                "what its module measured"
            )
        return 0

    if args.record_runner:
        reports = [log.read_text(errors="replace") for log in args.record_runner]
        written = write_runner_weights(reports, census())
        print(f"recorded {len(written)} runner weights to {RUNNER_WEIGHTS_PATH}")
        return 0
    if args.refresh_runner is not None:
        if args.refresh_runner < 1:
            parser.error("--refresh-runner needs at least one run")
        reports = fetch_runner_logs(args.refresh_runner)
        moved = refresh_runner_weights(reports, census())
        print(
            f"read {len(reports)} pytest log(s) from {args.refresh_runner} green run(s); "
            f"wrote {len(moved)} runner weight(s){': ' + ', '.join(moved) if moved else ''} "
            f"in {RUNNER_WEIGHTS_PATH}"
        )
        return 0
    if args.leg_spread is not None:
        print(leg_spread(green_pytest_legs(args.leg_spread)))
        return 0
    if args.summary:
        print(_summary(args.of))
        return 0
    if args.shard is None:
        parser.error(
            "one of --shard, --summary, --record-runner, --refresh-runner, --leg-spread or "
            "--check-durations is required"
        )

    selection = select(args.changed_since) if args.changed_since else None
    whole = selection is None or selection.full
    files = leg_files(args.shard, args.of, selection)
    # An empty share of the census is not a fast green: pytest exits 5 ("no tests collected")
    # on one, and a shard that collects nothing has silently stopped covering whatever it used
    # to hold. A selection's spare leg is empty on purpose.
    if whole and not files:
        raise SystemExit(f"shard {args.shard} of {args.of} selects no test files")
    text = "".join(f"{f}\n" for f in files)
    if args.out:
        args.out.write_text(text)
        why = selection.reason if selection else "no base to diff against"
        print(
            f"shard {args.shard} of {args.of}: {len(files)} test file(s) of "
            f"{'the whole census' if whole else 'a selection'}: {why}"
        )
    else:
        sys.stdout.write(text)
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a") as handle:
            handle.write(
                f"count={len(files)}\nscope={'full' if whole else 'selected'}\n"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
