#!/usr/bin/env python3
"""Split the pytest suite into N fixed shards of whole test modules, for CI's matrix.

WHY. The `pytest` job is the pole of every CI run and a landing waits on it twice — once on
the PR, once on the master merge commit that `land.sh` and the deployer's CI gate both read.
Sharding across matrix jobs is the remaining lever (#1270).

WHOLE MODULES, NEVER PART OF ONE. `--dist loadscope` in `addopts` keeps a module's tests on
one xdist worker so a module-scoped fixture is built once. Splitting a module across shards
would rebuild those fixtures per shard and give the saving straight back.

WHY WEIGHTS, AND NOT A HASH. A stable hash of the module path is the obvious split and it
balances badly here: the suite's cost is concentrated, not flat. Measured 2026-09-06 against a
serial `--durations=0` run of the whole suite (103.6s over 505 files), projected pole shard at
N=4 was 42.7s for a sha256 split and 39.8s for a greedy split weighted by the number of `def
test_` in each file, against 24.9s for a greedy split weighted by measured per-file seconds —
an even split being 25.9s. File byte size fared no better than the hash (36.2s). So the split
is longest-processing-time bin packing over recorded per-file durations.

STALENESS IS GRACEFUL, WHICH IS WHY THE RECORDED FILE IS SAFE TO COMMIT. A path with no
recorded weight is given the median of the recorded ones, and a recorded path that no longer
exists is ignored. Both cases cost balance and nothing else: every collected file still lands
in exactly one shard, so a stale table makes CI slower, never wrong.
`test_pytest_shards_partition_the_suite.py` fails when the recorded table has drifted far
enough that the balance is guesswork.

THE MEDIAN IS A BAD GUESS FOR A HEAVY FILE, AND THE TABLE CANNOT KNOW WHICH FILES ARE HEAVY.
The suite's median is 0.0s, so an unweighted file is packed as the lightest thing in the
suite and placed last, into whichever shard happened to be least loaded. One unrecorded 30s
module landed that way on 2026-09-17 and skewed the four CI shards to 65/99/99/68s against
a 43s projection for every one (#2225). `--record-missing` measures only the unweighted files
-- seconds, not the four-minute full run -- so the ratchet can afford to demand it as soon
as an unweighted file appears beside a recorded pole.

AND THE NEIGHBOURS CANNOT KNOW EITHER, WHEN THERE ARE NO NEIGHBOURS. That ratchet arm reads a
directory that already holds a recorded pole. A heavy module landing where every recorded
sibling is light is invisible to it, and every static proxy tried on the 2026-09-17 module --
`def test_` count, byte size, directory mean -- failed to separate it from an ordinary file
(#2238). `--check-durations` closes that: CI's own test step measures the module as a side
effect of running it, and the gate rejects an unweighted file that cost `RUNNER_HEAVY_SECONDS`
or more. It is a CI step rather than a pytest test on purpose -- see RATCHET_NODE_ID.

AND A RECORDED NUMBER CAN GO WRONG IN THE OTHER DIRECTION. All three arms above ask whether a
file is MISSING from the table; none asks whether a number still matches what the file costs.
`ansible/tests/k8s/test_secret_consumer_census.py` stood at 17.31s against 0.09s measured, and
the greedy split placed it first, so one shard carried 17s of phantom weight (#2514). The same
`--check-durations` report answers that too, as a RATIO -- see STALE_WEIGHT_RATIO -- and
`--record-files` is its repair, since `--record-missing` only fills gaps.

Usage:
    uv run python scripts/dev/pytest_shard.py --of 4 --shard 1        # this shard's files
    uv run python scripts/dev/pytest_shard.py --of 4 --shard 1 --out list.txt
    uv run python scripts/dev/pytest_shard.py --of 4 --summary        # projected balance
    uv run python scripts/dev/pytest_shard.py --record                # re-measure the weights
    uv run python scripts/dev/pytest_shard.py --record-missing        # only the unweighted files
    uv run python scripts/dev/pytest_shard.py --record-files a.py b.py  # refresh these entries
    uv run python scripts/dev/pytest_shard.py --check-durations ci.log  # CI's measured gate
"""

import argparse
import json
import re
import statistics
import subprocess
import sys
import tomllib
from pathlib import Path, PurePosixPath

# Reach `scripts/lib`: a directly-invoked script gets only its own directory on sys.path,
# and pyproject's `pythonpath` is a pytest setting.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.git import git

REPO = Path(__file__).resolve().parents[2]
WEIGHTS_PATH = Path(__file__).resolve().with_name("pytest_shard_weights.json")
# The coverage ratchet over this table. `record_weights` deselects it; see the reason there.
RATCHET_TEST = "ansible/tests/repo/test_pytest_shards_partition_the_suite.py"
# The ratchet's node id, spelled once. `record_weights` passes it to `--deselect`, and the two
# crons that commit with the prek chain on (docs-refresh, eval-run) put the same `--deselect`
# in PYTEST_ADDOPTS around their commit: while the ratchet is red, only a manual `--record`
# clears it, and until then it failed both crons' commits -- the docs stopped publishing and
# the failure path parked the deployer (#1899). CI's sharded job sets no PYTEST_ADDOPTS, so
# the ratchet stays enforced there. `test_the_crons_deselect_the_ratchet_they_cannot_repair`
# pins the templates to this string.
#
# ONE NODE ID, AND THAT IS WHY `--check-durations` IS A CI STEP. Both crons deselect exactly
# this one id. A second pytest test that went red on an unweighted file -- which is what a
# measured gate expressed as a test would be -- would fail those crons' commits again the way
# #1899 did, and neither cron can run the repair. So the measured verdict lives in `ci.yml` as
# a `run:` step instead, where no cron's commit passes through it (#2238).
RATCHET_NODE_ID = (
    f"{RATCHET_TEST}::test_the_recorded_weights_still_cover_most_of_the_suite"
)

# pytest's default `python_files`, both forms — the same pair
# `ansible/tests/repo/test_testpaths_covers_every_test_file.py` derives its census from, and for
# the same reason: a single hand-kept glob would miss a `foo_test.py` that pytest collects.
_TEST_FILE_GLOBS = ("test_*.py", "*_test.py")

# What a file costs before any of its tests run: pytest imports the module at collection, and
# the durations report attributes that to no test. Measured 2026-09-06 by timing two N=4 shards
# serially against their recorded totals — a 49-file shard ran 26.0s against 24.5s recorded and
# a 357-file shard 34.2s against the same 24.5s, so the residual is 0.031s and 0.027s per file.
# Without this term the split balances recorded seconds and leaves the file COUNT wildly uneven,
# which is how the 357-file shard became the pole while the model called every shard equal.
_PER_FILE_SECONDS = 0.028

# A line of pytest's durations report: seconds, phase, then a nodeid whose leading segment,
# up to the first pair of colons, is the test file this weight belongs to.
_DURATION_LINE = re.compile(r"^([0-9.]+)s\s+(call|setup|teardown)\s+(\S+?)::")

# What an unweighted module may measure on the CI runner before the shard gate rejects it.
#
# WHY A CI MEASUREMENT AND NOT ANOTHER STATIC ARM. The ratchet's neighbour arm sees an
# unweighted file only where a recorded pole already sits in its directory. A heavy module
# landing in a quiet directory is invisible to it, and no static proxy separates the two:
# `def test_` count, byte size and directory mean were all checked against the 2026-09-17
# module (6 tests, 248 lines, a directory averaging 0.19s) and none discriminates (#2238).
# Only running the file says what it costs, and CI already runs it.
#
# WHY TEN. The gate reads the runner's own per-test seconds, summed per file, where the table
# holds workstation seconds — the two are not the same scale, so the number is set against the
# shard it would skew rather than against the table. A shard projects around 40s at six ways,
# so an unweighted module worth 10s is a quarter of a shard placed by a 0.0s guess, which is
# the #2225 shape. The 2026-09-17 module that prompted all of this measured about 30s. The
# runner is slower per test than the workstation, so 10 runner seconds is FEWER than 10
# recorded seconds: the gate sits at or below the neighbour arm's own pole cutoff, which was
# 5.22s on 2026-09-22.
RUNNER_HEAVY_SECONDS = 10.0

# How many times its measured runner seconds a RECORDED weight may claim before the shard gate
# rejects it, and the recorded seconds below which nobody cares.
#
# WHY A SECOND ARM AT ALL. Every other arm of the gate asks "is this file missing from the
# table". None asks "is a recorded number still roughly what the file costs", so an entry that
# has become far too high stands until someone runs a full `--record`: `--record-missing` only
# fills gaps. Measured 2026-09-24 while fixing #2498,
# `ansible/tests/k8s/test_secret_consumer_census.py` was recorded at 17.31s and measured 0.09s.
# The greedy split placed it first, so one shard carried 17s of phantom weight for as long as
# the entry stood — the #2225 skew with the sign flipped (#2514).
#
# WHY A RATIO AND NOT A DELTA. The table holds workstation seconds and the report holds runner
# seconds, and the two are not the same scale. Controls measured on daniel-server 2026-09-24
# ran 1.10x to 1.46x their recorded values, so anything under about 3x is that scale
# difference rather than a stale entry. FIVE leaves room above that noise while still catching
# the 190x case, and a gate set nearer the noise would turn into a weekly chore.
#
# WHY A FLOOR ON THE RECORDED VALUE. The parser's own floor is pytest's 0.005s, so a file
# recorded at 0.05s and measuring 0.005s is a 10x ratio and pure rounding. The floor is what
# makes this arm about shard balance rather than about noise: a shard projects around 40s at
# six ways, and 17 of the 795 recorded files clear 3s. Those are the files whose placement
# decides the pole.
#
# THE OTHER WAY IT CAN FIRE, which is the gate being right rather than wrong: a module whose
# expensive tests SKIP on the runner and run on the workstation measures a fraction of its
# recorded weight there. The shard does not pay that cost on the runner either, so the entry is
# wrong for the split's purpose; `--record-files` from a checkout where the same tests skip is
# what records it honestly.
#
# THE BLIND SPOT. A file whose every test measures under pytest's `--durations-min` contributes
# no line and cannot be compared at all, so an entry that collapsed to literally nothing is
# invisible here. The #2498 entry measured 0.09s and did produce lines, which is the shape that
# has actually occurred.
STALE_WEIGHT_RATIO = 5.0
STALE_WEIGHT_FLOOR_SECONDS = 3.0

# pytest's own `--durations-min`: no report line is smaller, so no measured total is either.
# Used as the divisor's floor, which cannot then be zero.
_DURATIONS_MIN_SECONDS = 0.005

# The repair for every arm that finds a MISSING entry, spelled once and read by the ratchet test
# too, so the two can never offer different instructions. `census()` reads
# `git ls-files`, so the file has to be staged before the measurement can see it.
RECORD_MISSING_HINT = (
    "stage the new file, then `uv run python scripts/dev/pytest_shard.py "
    "--record-missing` and commit scripts/dev/pytest_shard_weights.json"
)

# The repair for the stale arm, which `--record-missing` cannot do: it only fills gaps, and a
# present-but-wrong entry is not a gap. A full `--record` re-measures the whole suite in
# minutes; `--record-files` re-measures the named files in seconds, so the gate has a repair
# cheap enough that nobody reaches for the deselect instead.
RECORD_FILES_HINT = (
    "re-measure just those files with `uv run python scripts/dev/pytest_shard.py "
    "--record-files <path>...` and commit scripts/dev/pytest_shard_weights.json"
)


def testpaths() -> list[str]:
    data = tomllib.loads((REPO / "pyproject.toml").read_text())
    paths = data["tool"]["pytest"]["ini_options"]["testpaths"]
    assert paths, "pyproject.toml declares no testpaths"
    return paths


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
    roots = [PurePosixPath(p) for p in testpaths()]
    return sorted(
        rel
        for rel in listed.split("\0")
        if rel
        and any(PurePosixPath(rel).match(g) for g in _TEST_FILE_GLOBS)
        and any(PurePosixPath(rel).is_relative_to(root) for root in roots)
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

    The coverage ratchet is DESELECTED because it is the thing this run repairs. It fails
    exactly when the weights have drifted far enough to need re-recording, and the refusal
    below treats any failure as "not a baseline" — so the suite could never go green and
    `--record` could never write. Measured 2026-09-11: 126 of 629 files unweighted, the
    ratchet red, and two consecutive `--record` runs wrote nothing.

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
            "--deselect",
            RATCHET_NODE_ID,
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


def parse_durations(text: str) -> dict[str, float]:
    """Per-file seconds summed out of a pytest `--durations` report.

    One parser for both readers: `measure_weights` above, which runs pytest itself, and
    `heavy_unweighted` below, which reads the log CI's own test step already produced. A
    second parser would drift from the first and the drift would read green.

    Every phase of every test counts — `setup` and `teardown` are what an expensive
    module-scoped fixture costs, and `--dist loadscope` exists precisely because those
    dominate some modules.
    """
    totals: dict[str, float] = {}
    for line in text.splitlines():
        match = _DURATION_LINE.match(line.strip())
        if match:
            totals[match.group(3)] = totals.get(match.group(3), 0.0) + float(
                match.group(1)
            )
    return {k: round(v, 3) for k, v in totals.items()}


def heavy_unweighted(
    text: str,
    weights: dict[str, float],
    threshold: float = RUNNER_HEAVY_SECONDS,
) -> list[tuple[str, float]]:
    """The modules in a durations report that cost `threshold`+ and have no recorded weight.

    Heaviest first. The report names only the files that shard actually ran, so no shard flags
    a file it did not run and the caller needs no separate file list.

    A file whose every test measures under pytest's `--durations-min` (0.005s by default)
    contributes no line and cannot be seen here. It also cannot reach the threshold: 10s of
    sub-5ms tests is 2000 of them in one module.
    """
    measured = parse_durations(text)
    return sorted(
        ((f, s) for f, s in measured.items() if f not in weights and s >= threshold),
        key=lambda item: (-item[1], item[0]),
    )


def stale_overweight(
    text: str,
    weights: dict[str, float],
    ratio: float = STALE_WEIGHT_RATIO,
    floor: float = STALE_WEIGHT_FLOOR_SECONDS,
) -> list[tuple[str, float, float]]:
    """The modules whose recorded weight is `ratio`x or more of what they measured here.

    `(path, recorded, measured)`, heaviest recorded first. Only entries recorded at `floor`
    seconds or more are compared — see `STALE_WEIGHT_RATIO` for why both bounds are where they
    are, and for the one shape this cannot see.

    Over-recording only: the runner is slower per test than the recording workstation, so a
    measured value ABOVE its recorded one is the normal case and not this arm's subject.
    """
    measured = parse_durations(text)
    flagged = [
        (f, weights[f], measured[f])
        for f in measured
        if f in weights
        and weights[f] >= floor
        and weights[f] >= ratio * max(measured[f], _DURATIONS_MIN_SECONDS)
    ]
    return sorted(flagged, key=lambda item: (-item[1], item[0]))


def durations_problems(
    text: str,
    weights: dict[str, float] | None = None,
    threshold: float = RUNNER_HEAVY_SECONDS,
    ratio: float = STALE_WEIGHT_RATIO,
) -> list[str]:
    """What is wrong with a shard's durations report, as readable complaints.

    Two arms over the one report, and both are reported: a heavy module with NO recorded
    weight, and a recorded weight far HIGHER than what the module measured here. Each complaint
    carries its own repair, because the two repairs differ — `--record-missing` fills a gap and
    cannot refresh an entry that is merely wrong.

    A function rather than inline asserts in `main`, so the accept and reject halves can hand
    it a fixed report rather than needing a CI run.

    An EMPTY report is itself a complaint. The gate's whole subject is found by parsing, so a
    report format change would leave it passing over nothing forever — the vacuous-green shape
    this repo's rule on pattern-found subjects names.
    """
    if not parse_durations(text):
        return [
            "parsed no durations out of the report — has the format changed, or did the "
            "test step drop --durations=0?"
        ]
    weights = load_weights() if weights is None else weights
    problems = []
    if heavy := heavy_unweighted(text, weights, threshold):
        listed = ", ".join(f"{f} ({s:.1f}s)" for f, s in heavy)
        problems.append(
            f"measured {threshold:.0f}s or more on this runner with no recorded weight, so "
            f"the shard split packs it as the lightest thing in the suite: {listed}. "
            f"Repair (seconds): {RECORD_MISSING_HINT}"
        )
    if stale := stale_overweight(text, weights, ratio):
        listed = ", ".join(
            f"{f} (recorded {rec:.2f}s, measured {got:.2f}s)" for f, rec, got in stale
        )
        problems.append(
            f"recorded at {ratio:.0f}x or more of what it measured on this runner, so the "
            f"shard split reserves time the file does not cost: {listed}. "
            f"Repair (seconds): {RECORD_FILES_HINT}"
        )
    return problems


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

    Seconds rather than the full run's minutes, which is what lets the coverage ratchet
    demand a record as soon as one unweighted file could matter. A recorded path no longer in
    the census is dropped on the way through, so the table does not keep a directory looking
    heavy on the strength of a module that was deleted.
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

    The repair for a recorded weight that has drifted (#2514). `record_missing_weights` above
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
        "--threshold",
        type=float,
        default=RUNNER_HEAVY_SECONDS,
        help="seconds an unweighted module may measure under --check-durations",
    )
    parser.add_argument(
        "--stale-ratio",
        type=float,
        default=STALE_WEIGHT_RATIO,
        help="how many times its measured seconds a recorded weight may claim",
    )
    args = parser.parse_args(argv)

    if args.check_durations:
        problems = durations_problems(
            args.check_durations.read_text(errors="replace"),
            threshold=args.threshold,
            ratio=args.stale_ratio,
        )
        if problems:
            print("\n".join(problems))
            return 1
        print(
            f"no unweighted module measured {args.threshold:.0f}s or more in this shard, and "
            f"no recorded weight is {args.stale_ratio:.0f}x what its module measured"
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
