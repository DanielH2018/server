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

Usage:
    uv run python scripts/dev/pytest_shard.py --of 4 --shard 1        # this shard's files
    uv run python scripts/dev/pytest_shard.py --of 4 --shard 1 --out list.txt
    uv run python scripts/dev/pytest_shard.py --of 4 --summary        # projected balance
    uv run python scripts/dev/pytest_shard.py --record                # re-measure the weights
"""

import argparse
import json
import re
import statistics
import subprocess
import sys
import tomllib
from pathlib import Path, PurePosixPath

REPO = Path(__file__).resolve().parents[2]
WEIGHTS_PATH = Path(__file__).resolve().with_name("pytest_shard_weights.json")

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
    listed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
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


def record_weights(path: Path = WEIGHTS_PATH) -> dict[str, float]:
    """Re-measure per-file seconds from a serial run and write them out.

    `-n0` so the durations are not distorted by worker contention, and `-vv` so pytest prints
    every duration rather than hiding the ones under 5ms — a file whose tests are all fast
    still costs its import, and leaving it unrecorded would hand it the median instead.
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
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise SystemExit(
            f"the suite did not pass, so its durations are not a baseline:\n{proc.stdout[-4000:]}"
        )
    totals: dict[str, float] = {}
    for line in proc.stdout.splitlines():
        match = _DURATION_LINE.match(line.strip())
        if match:
            totals[match.group(3)] = totals.get(match.group(3), 0.0) + float(
                match.group(1)
            )
    if not totals:
        raise SystemExit(
            "parsed no durations out of the run — has the report format changed?"
        )
    rounded = {k: round(v, 3) for k, v in sorted(totals.items())}
    path.write_text(json.dumps(rounded, indent=1, sort_keys=True) + "\n")
    return rounded


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
    args = parser.parse_args(argv)

    if args.record:
        written = record_weights()
        print(f"recorded {len(written)} file weights to {WEIGHTS_PATH}")
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
