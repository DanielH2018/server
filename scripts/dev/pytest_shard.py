#!/usr/bin/env python3
"""Split the pytest suite into N fixed shards of whole test modules, for CI's matrix.

WHY. The `pytest` job is the pole of every CI run and a landing waits on it twice — once on
the PR, once on the master merge commit that `land.sh` and the deployer's CI gate both read.
Sharding across matrix jobs is the remaining lever.

WHOLE MODULES, NEVER PART OF ONE. `--dist loadscope` in `addopts` keeps a module's tests on
one xdist worker so a module-scoped fixture is built once. Splitting a module across shards
would rebuild those fixtures per shard and give the saving straight back.

WHY WEIGHTS, AND NOT A HASH. A stable hash of the module path is the obvious split and it
balances badly here: the suite's cost is concentrated, not flat. Measured against a
serial `--durations=0` run of the whole suite (103.6s over 505 files), projected pole shard at
N=4 was 42.7s for a sha256 split and 39.8s for a greedy split weighted by the number of `def
test_` in each file, against 24.9s for a greedy split weighted by measured per-file seconds —
an even split being 25.9s. File byte size fared no better than the hash (36.2s). So the split
is longest-processing-time bin packing over recorded per-file durations.

STALENESS IS GRACEFUL, WHICH IS WHY THE RECORDED FILE IS SAFE TO COMMIT. A path with no
recorded weight is given the median of the recorded ones, and a recorded path that no longer
exists is ignored. Both cases cost balance and nothing else: every collected file still lands
in exactly one shard, so a stale table makes CI slower, never wrong.

THE MEDIAN IS A BAD GUESS FOR A HEAVY FILE, AND THE TABLE CANNOT KNOW WHICH FILES ARE HEAVY.
The suite's median is 0.0s, so an unweighted file is packed as the lightest thing in the
suite and placed last, into whichever shard happened to be least loaded. One unrecorded 30s
module landing that way skews the four CI shards to 65/99/99/68s against
a 43s projection for every one.

ONE GATE GUARDS THE TABLE, AND IT IS A CI STEP: `--check-durations`. It reads the durations
report CI's own test step already wrote, so it measures each module by running it. That is what
lets it see the two shapes nothing static can. A heavy module landing where every recorded
sibling is light is invisible to a neighbour heuristic, and `def test_` count, byte size and
directory mean all fail to separate a heavy module from an ordinary file; the
gate annotates it at `RUNNER_HEAVY_SECONDS` and rejects it at `RUNNER_HEAVY_FAIL_SECONDS`
(both in `shard_weight_gate.py`, with the two arms).
And a recorded number can go wrong in the other direction, which no arm asking "is this file
MISSING" ever sees:
a recorded 17.31s against 0.09s measured makes the greedy split place the file first, so one
shard carries 17s of phantom weight. The gate
answers that as a RATIO — see `shard_weight_gate.STALE_WEIGHT_RATIO` — and `--record-files` is its repair, since
`--record-missing` only fills gaps.

The docs-refresh cron runs `--record-missing` itself, so gaps in the table fill without a human.

THE GATE FAILS ONLY WHERE SOMEONE CAN APPLY THE REPAIR. On a pull request the author re-records
and pushes. On a push to master nobody can, and a red master run still reaches the deployer's
CI gate and every session's `land.sh`. Both arms compare one wall-clock reading against a fixed
bound, and runner variance spans it: one module measured about 8s on its PR run and 15.6s on
the master run of the same code (#3604). So CI passes `--warn-only` on push and merge_group,
which prints each complaint as a `::warning::` annotation and exits zero. On a pull request
the same variance lands a module near the bound on either side of it from run to run, so an
unweighted module between the two bounds annotates there too (#4010).

Usage:
    uv run python scripts/dev/pytest_shard.py --of 4 --shard 1        # this shard's files
    uv run python scripts/dev/pytest_shard.py --of 4 --shard 1 --out list.txt
    uv run python scripts/dev/pytest_shard.py --of 4 --summary        # projected balance
    uv run python scripts/dev/pytest_shard.py --record                # re-measure the weights
    uv run python scripts/dev/pytest_shard.py --record-missing        # only the unweighted files
    uv run python scripts/dev/pytest_shard.py --record-files a.py b.py  # refresh these entries
    uv run python scripts/dev/pytest_shard.py --check-durations ci.log  # CI's measured gate
    uv run python scripts/dev/pytest_shard.py --check-durations ci.log --warn-only  # annotate, exit 0
"""

import argparse
import json
import statistics
import subprocess
import sys
import tomllib
from pathlib import Path, PurePosixPath

# Reach `scripts/lib`: a directly-invoked script gets only its own directory on sys.path,
# and pyproject's `pythonpath` is a pytest setting.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.git import git
from lib.repo_paths import REPO
from shard_weight_gate import (
    RUNNER_HEAVY_FAIL_SECONDS,
    RUNNER_HEAVY_SECONDS,
    STALE_WEIGHT_RATIO,
    durations_problems,
    durations_warnings,
    parse_durations,
)

WEIGHTS_PATH = Path(__file__).resolve().with_name("pytest_shard_weights.json")
# pytest's default `python_files`, both forms — the same pair
# `ansible/tests/repo/test_testpaths_covers_every_test_file.py` derives its census from, and for
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
    cover `scripts_extra/test_x.py`, and `ansible/roles/*/*/tests` covers every role's suite.
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


def load_weights(path: Path = WEIGHTS_PATH) -> dict[str, float]:
    if not path.exists():
        return {}
    return {k: float(v) for k, v in json.loads(path.read_text()).items()}


def assign(files, shards: int, weights: dict[str, float]) -> dict[str, int]:
    """Map each file to a shard index in `range(shards)`, greedy longest-processing-time.

    Deterministic: files are ordered by descending weight then by path, and a tie between two
    equally loaded shards goes to the lower index. The same census and the same weights give
    the same assignment on every machine.
    """
    if shards < 1:
        raise ValueError(f"shards must be >= 1, got {shards}")
    known = [weights[f] for f in files if f in weights]
    default = statistics.median(known) if known else 1.0
    cost = {f: weights.get(f, default) + _PER_FILE_SECONDS for f in files}
    load = [0.0] * shards
    placed: dict[str, int] = {}
    for path in sorted(files, key=lambda f: (-cost[f], f)):
        target = min(range(shards), key=lambda i: (load[i], i))
        placed[path] = target
        load[target] += cost[path]
    return placed


def shard_files(shard: int, shards: int, files=None, weights=None) -> list[str]:
    """The files for 1-based `shard` of `shards`."""
    if not 1 <= shard <= shards:
        raise ValueError(f"shard {shard} is outside 1..{shards}")
    files = census() if files is None else files
    weights = load_weights() if weights is None else weights
    placed = assign(files, shards, weights)
    return sorted(f for f, i in placed.items() if i == shard - 1)


def measure_weights(files: list[str] | None = None) -> dict[str, float]:
    """Per-file seconds from one serial pytest run over `files` (the whole suite when None).

    `-n0` so the durations are not distorted by worker contention, and `-vv` so pytest prints
    every duration rather than hiding the ones under 5ms — a file whose tests are all fast
    still costs its import, and leaving it unrecorded would hand it the median instead.

    Nothing is deselected. The refusal below treats any failure as "not a baseline", so a
    test that reads the weights this run has not written yet could block the recording that
    would fix it. No test reads the table for staleness, so the
    precondition cannot refuse to clear itself.

    A file whose every test addopts deselects (`-m 'not ui'`, which CI runs under too) has no
    durations line and is absent from the result; the callers record it at 0.0, since in the
    run the shards are balanced for it costs its import and nothing else. A subset made only
    of such files collects nothing, which pytest reports as exit 5 and is not a failure here.
    """
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-n0",
            "-vv",
            "--durations=0",
            "-p",
            "no:cacheprovider",
            *(files or []),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    nothing_collected = bool(files) and proc.returncode == 5
    if proc.returncode != 0 and not nothing_collected:
        raise SystemExit(
            f"the suite did not pass, so its durations are not a baseline:\n{proc.stdout[-4000:]}"
        )
    totals = parse_durations(proc.stdout)
    if not totals and not nothing_collected:
        raise SystemExit(
            "parsed no durations out of the run — has the report format changed?"
        )
    return totals


def _write_weights(weights: dict[str, float], path: Path) -> dict[str, float]:
    ordered = dict(sorted(weights.items()))
    path.write_text(json.dumps(ordered, indent=1, sort_keys=True) + "\n")
    return ordered


def record_weights(path: Path = WEIGHTS_PATH) -> dict[str, float]:
    """Re-measure every file's seconds from a serial run of the whole suite and write them out."""
    measured = measure_weights()
    return _write_weights({f: measured.get(f, 0.0) for f in census()}, path)


def record_missing_weights(path: Path = WEIGHTS_PATH) -> dict[str, float]:
    """Measure only the census files the table lacks, and write the merged table.

    Seconds rather than the full run's minutes, which is what lets the docs-refresh cron run
    this on every tick. A recorded path no longer in the census is dropped on the way
    through, so the table does not keep a weight for a module that was deleted.
    """
    files = census()
    known = load_weights(path)
    kept = {f: known[f] for f in files if f in known}
    missing = [f for f in files if f not in known]
    if missing:
        measured = measure_weights(missing)
        kept |= {f: measured.get(f, 0.0) for f in missing}
    return _write_weights(kept, path)


def record_named_weights(
    paths: list[str], path: Path = WEIGHTS_PATH
) -> dict[str, float]:
    """Re-measure exactly `paths` and write the merged table, refreshing entries that exist.

    The repair for a recorded weight that has drifted. `record_missing_weights` above
    deliberately leaves a present entry alone, so without this the only refresh is a full
    `--record` — the whole suite, in minutes, rewriting every other entry from the same run.

    A path outside the census is refused rather than recorded: the census reads `git ls-files`,
    so a typo and an unstaged file both land here, and silently writing an entry no shard can
    ever use is how the table grows rows nobody can explain.
    """
    files = census()
    if unknown := [p for p in paths if p not in files]:
        raise SystemExit(
            "not a committed test file under testpaths, so no shard runs it: "
            + ", ".join(sorted(unknown))
        )
    known = load_weights(path)
    kept = {f: known[f] for f in files if f in known}
    measured = measure_weights(paths)
    kept |= {f: measured.get(f, 0.0) for f in paths}
    return _write_weights(kept, path)


def _summary(shards: int) -> str:
    files, weights = census(), load_weights()
    placed = assign(files, shards, weights)
    lines = []
    for i in range(shards):
        members = [f for f, s in placed.items() if s == i]
        cost = sum(weights.get(f, 0.0) + _PER_FILE_SECONDS for f in members)
        lines.append(
            f"  shard {i + 1}: {len(members):>4} files, {cost:6.1f}s projected"
        )
    unknown = sum(1 for f in files if f not in weights)
    lines.append(f"  {len(files)} files, {unknown} with no recorded weight")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--of", type=int, default=4, help="how many shards in total")
    parser.add_argument("--shard", type=int, help="which 1-based shard to print")
    parser.add_argument(
        "--out", type=Path, help="write the file list here instead of stdout"
    )
    parser.add_argument(
        "--summary", action="store_true", help="print the projected balance"
    )
    parser.add_argument(
        "--record", action="store_true", help="re-measure and rewrite the weights"
    )
    parser.add_argument(
        "--record-missing",
        action="store_true",
        help="measure only the test files with no recorded weight and merge them in",
    )
    parser.add_argument(
        "--record-files",
        nargs="+",
        metavar="PATH",
        help="re-measure only these test files and refresh their recorded weights",
    )
    parser.add_argument(
        "--check-durations",
        type=Path,
        metavar="LOG",
        help=(
            "read a pytest --durations report and fail on a heavy unweighted module or a "
            "recorded weight far above what its module measured"
        ),
    )
    parser.add_argument(
        "--warn-only",
        action="store_true",
        help=(
            "print --check-durations complaints as GitHub ::warning:: annotations and exit 0, "
            "for a run where nobody can apply the repair"
        ),
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=RUNNER_HEAVY_SECONDS,
        help="seconds an unweighted module may measure under --check-durations unannotated",
    )
    parser.add_argument(
        "--fail-above",
        type=float,
        default=RUNNER_HEAVY_FAIL_SECONDS,
        help=(
            "seconds an unweighted module may measure under --check-durations before the "
            "gate fails rather than annotates"
        ),
    )
    parser.add_argument(
        "--stale-ratio",
        type=float,
        default=STALE_WEIGHT_RATIO,
        help="how many times its measured seconds a recorded weight may claim",
    )
    args = parser.parse_args(argv)

    if args.check_durations:
        report = args.check_durations.read_text(errors="replace")
        weights = load_weights()
        problems = durations_problems(
            report,
            weights,
            threshold=args.threshold,
            ratio=args.stale_ratio,
            fail_above=args.fail_above,
        )
        warnings = durations_warnings(
            report, weights, threshold=args.threshold, fail_above=args.fail_above
        )
        if args.warn_only:
            warnings, problems = warnings + problems, []
        if warnings:
            print("\n".join(f"::warning::{w}" for w in warnings))
        if problems:
            print("\n".join(problems))
            return 1
        if not warnings:
            print(
                f"no unweighted module measured {args.threshold:.0f}s or more in this shard, "
                f"and no recorded weight is {args.stale_ratio:.0f}x what its module measured"
            )
        return 0

    if args.record:
        written = record_weights()
        print(f"recorded {len(written)} file weights to {WEIGHTS_PATH}")
        return 0
    if args.record_missing:
        known = load_weights()
        missing = [f for f in census() if f not in known]
        written = record_missing_weights()
        print(
            f"recorded {len(missing)} unweighted file(s); {len(written)} in the table at "
            f"{WEIGHTS_PATH}"
        )
        return 0
    if args.record_files:
        written = record_named_weights(args.record_files)
        refreshed = ", ".join(
            f"{f} ({written[f]:.2f}s)" for f in sorted(args.record_files)
        )
        print(f"re-measured {refreshed}; {len(written)} in the table at {WEIGHTS_PATH}")
        return 0
    if args.summary:
        print(_summary(args.of))
        return 0
    if args.shard is None:
        parser.error("one of --shard, --summary or --record is required")

    files = shard_files(args.shard, args.of)
    # An empty list is not a fast green: pytest exits 5 ("no tests collected") on one, and a
    # shard that collects nothing has silently stopped covering whatever it used to hold.
    if not files:
        raise SystemExit(f"shard {args.shard} of {args.of} selects no test files")
    text = "\n".join(files) + "\n"
    if args.out:
        args.out.write_text(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
