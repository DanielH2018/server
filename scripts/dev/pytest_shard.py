#!/usr/bin/env python3
"""Split the pytest suite into N fixed shards of whole test modules, for CI's matrix.

WHY. The `pytest` job is the pole of every CI run and a landing waits on it twice — once on
the PR, once on the master merge commit that `land.sh` and the deployer's CI gate both read.
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
longest-processing-time bin packing over per-file durations. It packs the runner seconds in
`pytest_shard_runner_weights.json` where that table has the module, and scales the recorded
weight otherwise; `shard_weight_gate.RUNNER_WEIGHTS_PATH` says why there are two tables.

STALENESS IS GRACEFUL, WHICH IS WHY THE RECORDED FILE IS SAFE TO COMMIT. A path with no
recorded weight is given the median of the recorded ones, and a recorded path that no longer
exists is ignored. Both cases cost balance and nothing else: every collected file still lands
in exactly one shard, so a stale table makes CI slower, never wrong.

THE MEDIAN IS A BAD GUESS FOR A HEAVY FILE, AND THE TABLE CANNOT KNOW WHICH FILES ARE HEAVY.
The suite's median is 0.0s, so an unweighted file is packed as the lightest thing in the
suite and placed last, into whichever shard happened to be least loaded. One unrecorded 30s
module landing that way skews the four CI shards to 65/99/99/68s against a 43s projection.

ONE GATE GUARDS THE TABLE, AND IT IS A CI STEP: `--check-durations`. It reads the durations
report CI's own test step already wrote, so it measures each module by running it. That is what
lets it see the two shapes nothing static can. A heavy module landing where every recorded
sibling is light is invisible to a neighbour heuristic, and `def test_` count, byte size and
directory mean all fail to separate a heavy module from an ordinary file; the
gate annotates it at `RUNNER_HEAVY_SECONDS` and rejects it at `RUNNER_HEAVY_FAIL_SECONDS`
(both in `shard_weight_gate.py`, with the arms).
And a recorded number can go wrong in the other direction, which no arm asking "is this file
MISSING" ever sees:
a recorded 17.31s against 0.09s measured makes the greedy split place the file first, so one
shard carries 17s of phantom weight. The gate
answers that as a RATIO: it annotates at `shard_weight_gate.STALE_WEIGHT_RATIO` and fails at
`STALE_WEIGHT_FAIL_RATIO`, since a host busy with fan-out batches records up to about 9x what the
runner measures (#5616). `--record-files` is its repair, since `--record-missing` only fills
gaps. A third arm takes the opposite direction: a module that grew
after its weight was recorded measured 45.67s against 0.48s, and the split packed it as free.
`shard_weight_gate.UNDER_WEIGHT_RATIO` bounds it, and `--record-files` repairs it too.

A CACHE SHARED ACROSS MODULES IS A PER-WORKER COST, NOT A MODULE'S. Under xdist every worker
builds `_k8s_render`'s render once and charges it to whichever module asks first, so its weight
would vary from run to run. `ansible/tests/conftest.py` builds it when collection finishes,
outside every test's timed phases, and its `# DECIDED:` comment has the numbers.

The docs-refresh cron runs `--record-missing` itself, so gaps in the table fill without a human.

A WEIGHT THAT DRIFTS MODERATELY LOW IS INVISIBLE TO THE GATE, so the cron re-measures it too.
Three modules measured 1.66x to 1.98x their recorded weight in a serial run (#4888), well
inside the 20x under-recorded arm, and `--record-missing` never touches a present entry. A CI
ratio that low cannot separate drift from runner scale, because the runner already measures
correctly recorded entries 1.1x to 13x their weight. A serial run on the recording host can, so
the cron runs `--refresh-drifted`: each run re-measures one hash slice of the table and rewrites
only the entries `DRIFT_RATIO` or more away from what they measured. Measurement noise inside
that band leaves the table alone, which is what keeps a twice-daily run from churning it.
Noise outside the band is caught by a second run of only the drifted files: an entry is
rewritten only when both runs drift and agree with each other within the band. One outlying
run wrote 0.0s for a 16s module and 62s for a 1s one, and CI failed the bot's branch (#6286).

THE GATE FAILS ONLY WHERE SOMEONE CAN APPLY THE REPAIR. On a pull request the author re-records
and pushes. On a push to master nobody can, and a red master run still reaches the deployer's
CI gate and every session's `land.sh`. Both arms compare one wall-clock reading against a fixed
bound, and runner variance spans it: one module measured about 8s on its PR run and 15.6s on
the master run of the same code (#3604). So CI passes `--warn-only` on push and merge_group,
which prints each complaint as a `::warning::` annotation and exits zero. On a pull request
the same variance lands a module near the bound on either side of it from run to run, so an
unweighted module between the two bounds annotates there too (#4010). A pull request's author
can repair only a module the PR's diff adds or changes, so CI passes the diff as
`--changed-files` and every other module's complaint annotates there as well (#5480). A change
to the table widens that to the rows it adds, changes or drops, which `--base-weights` lets
the gate read, and no further: a row the PR did not write is not its author's (#6266). A
change to the gate's own code widens it to every module.

Usage:
    uv run python scripts/dev/pytest_shard.py --of 4 --shard 1        # this shard's files
    uv run python scripts/dev/pytest_shard.py --of 4 --shard 1 --out list.txt
    uv run python scripts/dev/pytest_shard.py --of 4 --summary        # projected balance
    uv run python scripts/dev/pytest_shard.py --record                # re-measure the weights
    uv run python scripts/dev/pytest_shard.py --record-missing        # only the unweighted files
    uv run python scripts/dev/pytest_shard.py --record-files a.py b.py  # refresh these entries
    uv run python scripts/dev/pytest_shard.py --refresh-drifted 8     # one eighth, drifted rows only
    uv run python scripts/dev/pytest_shard.py --refresh-runner 3      # runner table from CI's logs
    uv run python scripts/dev/pytest_shard.py --check-durations ci.log  # CI's measured gate
    uv run python scripts/dev/pytest_shard.py --check-durations ci.log --warn-only  # annotate, exit 0
    uv run python scripts/dev/pytest_shard.py --check-durations ci.log --changed-files changed.txt
"""

import hashlib
import json
import statistics
import subprocess
import sys
import time
import tomllib
from pathlib import Path, PurePosixPath

# Reach `scripts/lib`: a directly-invoked script gets only its own directory on sys.path,
# and pyproject's `pythonpath` is a pytest setting.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.cli_help import ArgumentParser

from lib import gh as gh_mod
from lib.git import git
from lib.repo_paths import REPO
from shard_weight_gate import (
    RUNNER_HEAVY_FAIL_SECONDS,
    RUNNER_HEAVY_SECONDS,
    RUNNER_WEIGHTS_PATH,
    STALE_WEIGHT_RATIO,
    gate_complaints,
    load_runner_weights,
    packing_table,
    parse_durations,
    runner_weights,
    write_runner_weights,
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

# How far apart a module's recorded and measured seconds may sit, either way, before
# `--refresh-drifted` rewrites the entry. It is #4888's Verify-by bound, so a refreshed table
# holds every module within it, and a re-measurement inside it is treated as noise.
DRIFT_RATIO = 1.5
# An entry within this many seconds of what it measured stays, whatever the ratio: 0.02s
# measuring 0.05s is 2.5x and changes no shard's balance.
DRIFT_FLOOR_SECONDS = 0.5
# The docs-refresh cron runs at 06:17 and 18:17. Two runs 12 hours apart always fall in
# consecutive periods of this length, so successive runs visit successive slices.
_SLICE_PERIOD_SECONDS = 12 * 60 * 60

# Weights only CI can measure, which every write to the table keeps. A module whose expensive
# legs skip on every host here and run on CI records about 0s from any local re-measure, so
# `--record-files`, the gate's own repair, would write back the number the gate rejected, and
# `--refresh-drifted` would undo a hand fix within days (#5756). Set a pin to the sum of the
# module's lines in CI's `--durations` report, then run `--record-files` on it so the table,
# which is all CI reads, carries the pin too.
CI_ONLY_WEIGHTS = {
    # Four legs run the jobs image under docker, which no host here has. CI hands them the real
    # PATH through `ansible/tests/leakguard.py:_LIVE_API_TESTS`. 22.8s on master run 39813591673.
    "ansible/roles/k8s/crowdsec/tests/test_jobs_image.py": 22.8,
}


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
    weights = packing_table(load_weights()) if weights is None else weights
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


def _write_weights(
    weights: dict[str, float], path: Path, pins: dict[str, float] = CI_ONLY_WEIGHTS
) -> dict[str, float]:
    pinned = {f: w for f, w in pins.items() if f in weights}
    ordered = dict(sorted((weights | pinned).items()))
    path.write_text(json.dumps(ordered, indent=1, sort_keys=True) + "\n")
    return ordered


def record_weights(
    path: Path = WEIGHTS_PATH,
    *,
    files: list[str] | None = None,
    measure=measure_weights,
    pins: dict[str, float] = CI_ONLY_WEIGHTS,
) -> dict[str, float]:
    """Re-measure every file's seconds from a serial run of the whole suite and write them out."""
    measured = measure()
    files = census() if files is None else files
    return _write_weights({f: measured.get(f, 0.0) for f in files}, path, pins)


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
    paths: list[str],
    path: Path = WEIGHTS_PATH,
    *,
    files: list[str] | None = None,
    measure=measure_weights,
    pins: dict[str, float] = CI_ONLY_WEIGHTS,
) -> dict[str, float]:
    """Re-measure exactly `paths` and write the merged table, refreshing entries that exist.

    The repair for a recorded weight that has drifted. `record_missing_weights` above
    deliberately leaves a present entry alone, so without this the only refresh is a full
    `--record` — the whole suite, in minutes, rewriting every other entry from the same run.

    A path outside the census is refused rather than recorded: the census reads `git ls-files`,
    so a typo and an unstaged file both land here, and silently writing an entry no shard can
    ever use is how the table grows rows nobody can explain.
    """
    files = census() if files is None else files
    if unknown := [p for p in paths if p not in files]:
        raise SystemExit(
            "not a committed test file under testpaths, so no shard runs it: "
            + ", ".join(sorted(unknown))
        )
    known = load_weights(path)
    kept = {f: known[f] for f in files if f in known}
    measured = measure(paths)
    kept |= {f: measured.get(f, 0.0) for f in paths}
    return _write_weights(kept, path, pins)


def refresh_slice(path: str, slices: int) -> int:
    """The 0-based slice of `slices` that `--refresh-drifted` re-measures `path` in.

    A hash of the path, not `assign`'s weighted packing. Packing moves a module to another
    slice whenever any weight changes, so a module could keep landing in slices already visited
    and go unmeasured for several rotations. A hash places it by its own path alone.
    """
    return int(hashlib.sha256(path.encode()).hexdigest(), 16) % slices


def slice_at(seconds: float, slices: int) -> int:
    """The 0-based slice a `--refresh-drifted` run at epoch `seconds` re-measures."""
    return int(seconds // _SLICE_PERIOD_SECONDS) % slices


def drifted(recorded: float, measured: float) -> bool:
    """Whether `measured` is far enough from `recorded`, either way, to rewrite the entry."""
    if abs(measured - recorded) < DRIFT_FLOOR_SECONDS:
        return False
    low, high = sorted((recorded, measured))
    return high > DRIFT_RATIO * low


def refresh_drifted_weights(
    index: int,
    slices: int,
    path: Path = WEIGHTS_PATH,
    *,
    files: list[str] | None = None,
    measure=measure_weights,
    pins: dict[str, float] = CI_ONLY_WEIGHTS,
) -> list[str]:
    """Re-measure slice `index` of `slices` of the recorded entries, and rewrite the drifted ones.

    The unattended repair for a weight that has drifted but not far enough for the gate's
    ratio arms. Only an entry `drifted` judges out of band is rewritten, so re-measuring a
    slice usually leaves the table byte-identical. A drifted entry is measured a second time,
    and it is rewritten with that measurement only when the two runs agree. A recorded path no longer in the census is
    dropped, as `record_missing_weights` drops it.

    Args:
        index: The 0-based slice to re-measure.
        slices: How many slices the table is split into.
        path: The weights table to read and rewrite.
        files: The census to keep entries for; `census()` when None.
        measure: Measures a list of files, as `measure_weights` does.
        pins: The CI-only weights, which are neither measured nor rewritten.

    Returns:
        The paths whose weights were rewritten, sorted.
    """
    files = census() if files is None else files
    known = load_weights(path)
    kept = {f: known[f] for f in files if f in known}
    chosen = [f for f in kept if refresh_slice(f, slices) == index and f not in pins]
    measured = measure(chosen) if chosen else {}
    candidates = [f for f in chosen if drifted(kept[f], measured.get(f, 0.0))]
    # One serial run is not enough to rewrite a row: a single outlier measured 0.0s for a 16s
    # module and 62s for a 1s one, and CI then failed the bot's own branch on it (#6286). The
    # drifted files run once more, and a row moves only when the second run drifts too and
    # agrees with the first within DRIFT_RATIO.
    confirmed = measure(candidates) if candidates else {}
    moved = sorted(
        f
        for f in candidates
        if drifted(kept[f], confirmed.get(f, 0.0))
        and not drifted(measured.get(f, 0.0), confirmed.get(f, 0.0))
    )
    kept |= {f: confirmed.get(f, 0.0) for f in moved}
    _write_weights(kept, path, pins)
    return moved


# Where `--refresh-runner` reads its logs: the green runs of CI's workflow on pushes to master.
# A pull request's run also measures modules that may never land, and a merge-queue run repeats
# a commit master runs again, so only the push runs count.
CI_WORKFLOW = "ci.yml"
CI_BRANCH = "master"
# Every pytest leg is named `pytest (shard N of M)` in ci.yml. A prefix rather than a count, so
# changing the matrix needs no edit here.
PYTEST_JOB_PREFIX = "pytest (shard "
# How many green master runs `fetch_runner_logs` reads through to find the ones that ran pytest.
# A push whose paths the matrix does not cover skips every leg: two of the newest three green
# runs on 2026-10-28 did. The listing is one call whatever its length, and each run read costs
# one more.
RUN_WINDOW = 30


def fetch_runner_logs(runs: int, gh=gh_mod.gh) -> list[str]:
    """The raw log of every pytest leg in the newest `runs` green master runs that ran them.

    Each call goes through `lib.gh`, which is the deploy user's authenticated CLI with a 60s
    bound: the docs-refresh cron holds the git-tree lock while this runs. Three runs of six
    legs is at most 1 + `RUN_WINDOW` + 18 calls, against the authenticated 5000 an hour.

    Raises:
        RuntimeError: fewer than `runs` of the newest `RUN_WINDOW` green runs carried a pytest
            log. A median over fewer
            runs is one noisy reading per module (#3604), so nothing is written from it.
        subprocess.CalledProcessError: any `gh` call failed. A missing log fails the whole
            fetch rather than leaving one shard's modules unmeasured.
    """
    list_args = (
        f"run list --workflow {CI_WORKFLOW} --branch {CI_BRANCH} --event push"
        f" --status success --limit {RUN_WINDOW} --json databaseId"
    ).split()
    listed = json.loads(gh(*list_args, cwd=REPO).stdout or "[]")
    reports, measured_runs = [], 0
    for run in listed:
        if measured_runs == runs:
            break
        view = gh("run", "view", str(run["databaseId"]), "--json", "jobs", cwd=REPO)
        legs = [
            job["databaseId"]
            for job in json.loads(view.stdout)["jobs"]
            if job["name"].startswith(PYTEST_JOB_PREFIX)
            and job["conclusion"] == "success"
        ]
        measured_runs += bool(legs)
        # The flag because ci.yml runs pytest with `--color yes`, and `gh api` refuses a body
        # carrying escape sequences without it. `runner_weights` strips the colours.
        reports += [
            gh(
                "api",
                "--allow-escape-sequences",
                f"repos/{{owner}}/{{repo}}/actions/jobs/{leg}/logs",
                cwd=REPO,
            ).stdout
            for leg in legs
        ]
    if measured_runs < runs:
        raise RuntimeError(
            f"found pytest logs in {measured_runs} of the newest {len(listed)} green "
            f"{CI_BRANCH} run(s) of {CI_WORKFLOW}, not the {runs} asked for"
        )
    return reports


def refresh_runner_weights(
    reports: list[str], keep: list[str], path: Path = RUNNER_WEIGHTS_PATH
) -> list[str]:
    """Merge the medians in `reports` into the runner table; return the modules it wrote.

    A merge, not `write_runner_weights`'s rewrite. The newest runs change with every merge to
    master, so a rewrite would move most rows on every cron run and publish a PR each time.
    Here a module the table lacks is added, a present one is rewritten only when `drifted`
    judges it out of band, and a module no longer in `keep` is dropped. A module the reports
    did not measure keeps its row, so a module that skipped on these runs is not packed cold.
    """
    measured = runner_weights(reports)
    if not measured:
        raise ValueError(
            "no durations line in any report, so there is nothing to record"
        )
    kept = set(keep)
    table = {m: s for m, s in load_runner_weights(path).items() if m in kept}
    moved = sorted(
        m
        for m, s in measured.items()
        if m in kept and (m not in table or drifted(table[m], s))
    )
    table |= {m: measured[m] for m in moved}
    path.write_text(json.dumps(table, indent=1, sort_keys=True) + "\n")
    return moved


def _summary(shards: int) -> str:
    files, weights = census(), packing_table(load_weights())
    placed = assign(files, shards, weights)
    lines = []
    for i in range(shards):
        members = [f for f, s in placed.items() if s == i]
        cost = sum(weights.get(f, 0.0) + _PER_FILE_SECONDS for f in members)
        lines.append(
            f"  shard {i + 1}: {len(members):>4} files, {cost:6.1f} runner seconds projected"
        )
    unknown = sum(1 for f in files if f not in weights)
    lines.append(f"  {len(files)} files, {unknown} with no recorded weight")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = ArgumentParser(description=__doc__.splitlines()[0])
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
        "--refresh-drifted",
        type=int,
        metavar="SLICES",
        help=(
            "re-measure one of SLICES hash slices of the recorded weights, chosen by the "
            "12-hour period, and rewrite the entries that drifted past DRIFT_RATIO"
        ),
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
        "--check-durations",
        type=Path,
        metavar="LOG",
        help=(
            "read a pytest --durations report and fail on a heavy unweighted module, a "
            "recorded weight far above what its module measured, or one far below it"
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
        "--changed-files",
        type=Path,
        metavar="LIST",
        help="the paths a PR's diff adds or modifies: only those modules fail the gate",
    )
    parser.add_argument(
        "--base-weights",
        type=Path,
        metavar="JSON",
        help="the weight table at the PR's merge base: only the rows a table edit wrote fail",
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
        help="seconds an unweighted module may measure before --check-durations fails",
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
        changed = args.changed_files and set(args.changed_files.read_text().split())
        problems, warnings = gate_complaints(
            report,
            load_weights(),
            changed,
            threshold=args.threshold,
            ratio=args.stale_ratio,
            fail_above=args.fail_above,
            base_weights=args.base_weights and load_weights(args.base_weights),
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
                f"no unweighted or under-recorded module measured {args.threshold:.0f}s or "
                f"more in this shard, and no recorded weight is {args.stale_ratio:.0f}x what "
                f"its module measured"
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
    if args.refresh_drifted is not None:
        slices = args.refresh_drifted
        if slices < 1:
            parser.error("--refresh-drifted needs at least one slice")
        index = slice_at(time.time(), slices)
        moved = refresh_drifted_weights(index, slices)
        print(
            f"re-measured slice {index + 1} of {slices}; rewrote {len(moved)} drifted "
            f"weight(s){': ' + ', '.join(moved) if moved else ''} in {WEIGHTS_PATH}"
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
    if args.summary:
        print(_summary(args.of))
        return 0
    if args.shard is None:
        parser.error(
            "one of --shard, --summary, --record, --record-missing, --record-files, "
            "--refresh-drifted, --record-runner, --refresh-runner or --check-durations "
            "is required"
        )

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
