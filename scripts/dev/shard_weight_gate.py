"""The measured shard-weight check: what CI's `pytest_shard.py --check-durations` step reports.

It reads the pytest `--durations` report a CI shard already wrote, and complains about three
shapes no static check can see: a heavy test module with no recorded weight, a recorded weight
far above what its module measured, and a heavy module whose recorded weight is far below it.
`pytest_shard.py`'s module docstring has why the split needs the table and why no complaint
fails the run. This module holds the bounds and the three arms; `pytest_shard.main` reads the
report, hands over the committed table and prints each complaint as an annotation.

Split out of `pytest_shard.py` when the margin band (#4010) took that module past the
600-line cap. The runner-seconds table that `pytest_shard.py` packs the shards on lives here
too, at the end, for the same reason (#6149), with the fetch of CI's pytest leg logs that both
the table and `leg_spread` read.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import json
import re
import statistics
from pathlib import Path

from lib import gh as gh_mod
from lib.repo_paths import REPO

# A line of pytest's durations report: seconds, phase, then a nodeid whose leading segment,
# up to the first pair of colons, is the test file this weight belongs to.
_DURATION_LINE = re.compile(r"^([0-9.]+)s\s+(call|setup|teardown)\s+(\S+?)::")

# What an unweighted module may measure on the CI runner before the shard gate annotates it.
#
# WHY A CI MEASUREMENT AND NOT A STATIC ARM. A neighbour heuristic sees an
# unweighted file only where a recorded pole already sits in its directory. A heavy module
# landing in a quiet directory is invisible to it, and no static proxy separates the two:
# `def test_` count, byte size and directory mean were all checked against a heavy module
# (6 tests, 248 lines, a directory averaging 0.19s) and none discriminates.
# Only running the file says what it costs, and CI already runs it.
#
# WHY TEN. The gate reads the runner's own per-test seconds, summed per file, where the table
# holds workstation seconds — the two are not the same scale, so the number is set against the
# shard it would skew rather than against the table. A shard projects around 40s at six ways,
# so an unweighted module worth 10s is a quarter of a shard placed by a 0.0s guess. The
# module that motivated the gate measured about 30s. The
# runner is slower per test than the workstation, so 10 runner seconds is FEWER than 10
# recorded seconds: the gate sits at or below the pole cutoff a neighbour heuristic would use.
RUNNER_HEAVY_SECONDS = 10.0

# How many times its measured runner seconds a RECORDED weight may claim before the shard check
# annotates it, and the recorded seconds below which nobody cares.
#
# WHY AN OVER-RECORDING ARM. The unweighted arm asks "is this file missing from the
# table". It never asks "is a recorded number still roughly what the file costs", so an entry that
# has become far too high stands until someone runs a full `--record`: `--record-missing` only
# fills gaps. A recorded 17.31s for `tests/k8s/test_secret_consumer_census.py` that
# measured 0.09s made the greedy split place it first, so one shard carried 17s of phantom
# weight for as long as the entry stood — the skew of an unweighted heavy file with the sign
# flipped.
#
# WHY A RATIO AND NOT A DELTA. The table holds workstation seconds and the report holds runner
# seconds, and the two are not the same scale. Controls measured on daniel-server
# ran 1.10x to 1.46x their recorded values, so anything under about 3x is that scale
# difference rather than a stale entry. FIVE leaves room above that noise; a busy recording
# host still overshoots it on its own (`LOCAL_READING_MAX_RATIO`).
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
# invisible here. An entry that is merely far too high still produces lines, which is the
# shape that occurs in practice.
STALE_WEIGHT_RATIO = 5.0
STALE_WEIGHT_FLOOR_SECONDS = 3.0

# How many times the runner table's seconds a local reading may claim before the docs-refresh
# cron treats it as a cost of the recording host alone. `--refresh-drifted` then keeps the
# recorded weight, and `--record-missing` writes the runner seconds.
#
# WHY TWENTY. A host busy with fan-out batches reads up to about nine times what the runner
# measures (#5616): `test_fanout_clean_chain.py` read 9.14s against 0.99s. A cost that is real on
# one host alone reads further off still, such as a test walking untracked files: 85.09s against
# CI's 2.08s (#7132). `UNDER_WEIGHT_RATIO` uses twenty for the same variance.
LOCAL_READING_MAX_RATIO = 20.0

# How many times its recorded weight a module may measure on the runner before the shard check
# reads the entry as too LOW. The arm annotates from `UNDER_WEIGHT_WARN_SECONDS` below.
#
# WHY A THIRD ARM. The stale arm above catches a recorded number far too high, and
# `--record-missing` never touches a present entry, so a weight far too low stood unseen. On
# the master run in #4873, `test_guard_tests_read_renders_not_templates.py` measured 45.67s
# against a recorded 0.48s and `test_nut_fsd_login_confined.py` 14.65s against 0.01s. Each
# module had grown after its weight was recorded, so the split packed it as free: the skew of
# an unweighted heavy file, with an entry present.
#
# WHY TWENTY. The runner is not a fixed multiple of the workstation. On that same run,
# `scripts/docs/tests/test_gen_reference_decisions.py` measured 36.15s against 5.73s serial
# on daniel-box, 6.3x, and #3604 saw one module vary 2x between two runs of the same code. A
# correctly recorded module can therefore reach about 13x, and twenty clears it. The entries
# this arm exists for were 34x to 1500x off.
UNDER_WEIGHT_RATIO = 20.0

# Where the under-recorded arm starts ANNOTATING, below the 10s the unweighted arm uses.
#
# WHY LOWER. #4873's verify-by is "no module recorded under 1s totals over 5s", and four of
# the six rows that issue named measured 7.41-7.66s against 0.0-0.01s recorded: under the 10s
# bound, so the arm could not have seen them. Those four were the k8s render charged to its
# first reader, which `tests/conftest.py` now builds outside every timed phase; this
# band is what makes that shape visible again if the build ever lands back on a test. Between
# here and `RUNNER_HEAVY_SECONDS` a 20x ratio needs a weight of 0.25-0.5s or less, so the band
# annotates only near-free entries.
UNDER_WEIGHT_WARN_SECONDS = 5.0

# pytest's own `--durations-min`: no report line is smaller, so no measured total is either.
# Used as the divisor's floor, which cannot then be zero.
_DURATIONS_MIN_SECONDS = 0.005

# The repair for the arm that finds a MISSING entry. `census()` reads `git ls-files`, so
# the file has to be staged before the measurement can see it.
RECORD_MISSING_HINT = (
    "stage the new file, then `uv run python scripts/dev/pytest_shard.py "
    "--record-missing` and commit scripts/dev/pytest_shard_weights.json"
)

# The repair for the stale arm, which `--record-missing` cannot do: it only fills gaps, and a
# present-but-wrong entry is not a gap. A full `--record` re-measures the whole suite in
# minutes; `--record-files` re-measures the named files in seconds.
RECORD_FILES_HINT = (
    "re-measure just those files with `uv run python scripts/dev/pytest_shard.py "
    "--record-files <path>...` and commit scripts/dev/pytest_shard_weights.json; for a "
    "path in pytest_shard.py's CI_ONLY_WEIGHTS, first set its pin to the measured seconds"
)


def parse_durations(text: str) -> dict[str, float]:
    """Per-file seconds summed out of a pytest `--durations` report.

    One parser for both readers: `pytest_shard.measure_weights`, which runs pytest itself, and
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


def over_recorded(
    recorded: float,
    measured: float,
    ratio: float = STALE_WEIGHT_RATIO,
    floor: float = STALE_WEIGHT_FLOOR_SECONDS,
) -> bool:
    """Whether a weight of `recorded` seconds is `ratio`x or more of `measured`, from `floor` up."""
    return recorded >= floor and recorded >= ratio * max(
        measured, _DURATIONS_MIN_SECONDS
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
    `heavy_underweighted` takes a measured value FAR above it.
    """
    measured = parse_durations(text)
    flagged = [
        (f, weights[f], measured[f])
        for f in measured
        if f in weights and over_recorded(weights[f], measured[f], ratio, floor)
    ]
    return sorted(flagged, key=lambda item: (-item[1], item[0]))


def heavy_underweighted(
    text: str,
    weights: dict[str, float],
    threshold: float = RUNNER_HEAVY_SECONDS,
    ratio: float = UNDER_WEIGHT_RATIO,
) -> list[tuple[str, float, float]]:
    """The recorded modules that measured `threshold`+ and `ratio`x or more their weight.

    `(path, recorded, measured)`, heaviest measured first. The under-recording counterpart of
    `stale_overweight`, bounded like `heavy_unweighted`; see `UNDER_WEIGHT_RATIO` for why.
    """
    measured = parse_durations(text)
    flagged = [
        (f, weights[f], s)
        for f, s in measured.items()
        if f in weights and s >= threshold and s >= ratio * weights[f]
    ]
    return sorted(flagged, key=lambda item: (-item[2], item[0]))


def _repair(hint: str) -> str:
    return (
        "The docs-refresh cron repairs it unattended within a day; to repair it in this PR "
        f"instead, {hint}"
    )


def _stale_complaint(stale: list[tuple[str, float, float]], ratio: float) -> str:
    listed = ", ".join(
        f"{f} (recorded {rec:.2f}s, measured {got:.2f}s)" for f, rec, got in stale
    )
    return (
        f"recorded at {ratio:.0f}x or more of what it measured on this runner, so the "
        f"shard split reserves time the file does not cost: {listed}. "
        f"{_repair(RECORD_FILES_HINT)}"
    )


def _heavy_complaint(heavy: list[tuple[str, float]], threshold: float) -> str:
    listed = ", ".join(f"{f} ({s:.1f}s)" for f, s in heavy)
    return (
        f"measured {threshold:.0f}s or more on this runner with no recorded weight, so "
        f"the shard split packs it as the lightest thing in the suite: {listed}. "
        f"{_repair(RECORD_MISSING_HINT)}"
    )


def _under_complaint(
    under: list[tuple[str, float, float]], threshold: float, ratio: float
) -> str:
    listed = ", ".join(
        f"{f} (recorded {rec:.2f}s, measured {got:.1f}s)" for f, rec, got in under
    )
    return (
        f"measured {threshold:.0f}s or more on this runner and {ratio:.0f}x or more its "
        f"recorded weight, so the shard split packs it as nearly free: {listed}. "
        f"{_repair(RECORD_FILES_HINT)}. If it records near 0s again, the time is a "
        "per-process cache its first test built: build that cache outside the timed phases, "
        "as tests/render_share.py does for the k8s and fleet renders"
    )


def durations_complaints(
    text: str,
    weights: dict[str, float],
    threshold: float = RUNNER_HEAVY_SECONDS,
    ratio: float = STALE_WEIGHT_RATIO,
    under_ratio: float = UNDER_WEIGHT_RATIO,
    under_threshold: float = UNDER_WEIGHT_WARN_SECONDS,
) -> list[str]:
    """What is wrong with a shard's durations report, as readable complaints.

    Three arms over the one report, and all are reported: a heavy module with NO recorded
    weight, a recorded weight far HIGHER than what the module measured here, and a module
    whose recorded weight is far LOWER. Each complaint carries its own repair, because the
    repairs differ: `--record-missing` fills a gap and cannot refresh an entry that is merely
    wrong.

    An EMPTY report is itself a complaint. The check's whole subject is found by parsing, so a
    report format change would leave it reporting nothing forever.
    """
    if not parse_durations(text):
        return [
            "parsed no durations out of the report — has the format changed, or did the "
            "test step drop --durations=0?"
        ]
    complaints = []
    if heavy := heavy_unweighted(text, weights, threshold):
        complaints.append(_heavy_complaint(heavy, threshold))
    if under := heavy_underweighted(text, weights, under_threshold, under_ratio):
        complaints.append(_under_complaint(under, under_threshold, under_ratio))
    if stale := stale_overweight(text, weights, ratio):
        complaints.append(_stale_complaint(stale, ratio))
    return complaints


# ── The runner-seconds table the shards are packed on (#6149) ─────────────────────────────────
#
# WHY A SECOND TABLE. `pytest_shard_weights.json` holds workstation seconds, and the runner does
# not scale them evenly. Over three CI runs on 2026-10-24 (40125170130, 40131095618,
# 40130851623), the middle 90% of the 290 modules recorded at 0.5s or more measured 1.0x to
# 5.5x their recorded weight on the runner, with a median of 2.3x. On run 40125170130 every
# shard recorded 157-159s, the runner measured them at 327-612s, and their test steps took 133s
# to 241s. Packing on what the runner measured projects every shard at 460-468 runner seconds
# over the same three runs.
#
# WHY NOT WRITE RUNNER SECONDS INTO THE WEIGHT TABLE. The docs-refresh cron's
# `--refresh-drifted` re-measures that table on daniel-box and rewrites any entry 1.5x away from
# what it measured, so runner seconds there would drift back within days (#5432). The shard
# check's ratio arms also compare that table with the runner on purpose, at bounds wide enough
# for the scale gap. So the weight table keeps feeding the check and the cron, and this one feeds
# only `assign`.
#
# STALENESS IS GRACEFUL, as with the weight table. A module this table lacks is packed at its
# recorded weight times the median runner-to-recorded ratio, and an entry for a deleted module
# is ignored. The docs-refresh cron refreshes it twice a day with `pytest_shard.py
# --refresh-runner 3`, which downloads every pytest leg's log from the three newest green master
# runs and rewrites only the rows that drifted (#6178). Each module takes the median of what the
# logs measured, because one module measured about 8s on one run and 15.6s on the next (#3604).
# `--record-runner LOG...` is the by-hand full rewrite from logs you downloaded yourself.
#
# EACH LEG'S READINGS ARE SCALED TO ONE REFERENCE RUNNER FIRST, because the hardware a leg draws
# sets the speed of every module in it. Over three master runs on 2026-11-08 (42529937412,
# 42532417368, 42532752393) one unchanged shard ran 0.67x its table on one leg and 1.22x on the
# next, and the six legs of one run spread 140s to 290s on a split projected flat (#7708). That
# spread is wider than `DRIFT_RATIO`, and every module in a shard shares it, so the medians of
# three runs moved 63 to 94 rows on each refresh, correlated by shard. The leg's own cold
# `uv sync` bytecode compile measures the same hardware before any test runs. Its seconds per
# file predicted the leg's factor within ±8% on all 18 legs, and scaling by it cut a module's
# spread across the three runs from a median of 1.41x to 1.12x.
_COMPILE_LINE = re.compile(r"Bytecode compiled (\d+) files in ([0-9.]+)(ms|s)\b")
# The cold compile's seconds per file on a leg that measures its modules at their table weight.
# A fixed constant, so the table's scale does not move with the runs a refresh happens to read.
REFERENCE_COMPILE_SECONDS_PER_FILE = 0.00085
# A compile faster than this per file found its bytecode already built, so it measures no CPU.
# Cold compiles measured 0.53-1.0 ms per file and warm ones 0.03-0.06 ms (#7708).
_COLD_COMPILE_FLOOR_SECONDS_PER_FILE = 0.0002
RUNNER_WEIGHTS_PATH = (
    Path(__file__).resolve().with_name("pytest_shard_runner_weights.json")
)
# How far apart a module's recorded and measured seconds may sit, either way, before
# `--refresh-drifted` rewrites the entry. It is #4888's Verify-by bound, so a refreshed table
# holds every module within it, and a re-measurement inside it is treated as noise.
DRIFT_RATIO = 1.5
# An entry within this many seconds of what it measured stays, whatever the ratio: 0.02s
# measuring 0.05s is 2.5x and changes no shard's balance.
DRIFT_FLOOR_SECONDS = 0.5

# A recorded weight below this is too small to give a meaningful runner-to-recorded ratio.
_RATIO_FLOOR_SECONDS = 0.5
# A GitHub job log prefixes each line with a timestamp, and pytest colours its report.
_LOG_NOISE = re.compile(r"^\S+Z |\x1b\[[0-9;]*m", re.M)


def drifted(recorded: float, measured: float) -> bool:
    """Whether `measured` is far enough from `recorded`, either way, to rewrite the entry."""
    if abs(measured - recorded) < DRIFT_FLOOR_SECONDS:
        return False
    low, high = sorted((recorded, measured))
    return high > DRIFT_RATIO * low


def leg_speed(text: str) -> float:
    """How many times slower than the reference runner the leg that wrote `text` ran.

    The slowest bytecode-compile line in the log is the cold `uv sync` one. A log without a
    cold compile reads 1.0, so its readings are taken as measured.
    """
    per_file = [
        float(seconds) / (1000 if unit == "ms" else 1) / int(files)
        for files, seconds, unit in _COMPILE_LINE.findall(text)
        if int(files)
    ]
    cold = max(per_file, default=0.0)
    if cold < _COLD_COMPILE_FLOOR_SECONDS_PER_FILE:
        return 1.0
    return cold / REFERENCE_COMPILE_SECONDS_PER_FILE


# pytest's closing line, `==== 3720 passed, 71 skipped in 261.46s (0:04:21) ====`.
_SUMMARY_WALL = re.compile(r"^=+ .*\bin ([0-9.]+)s\b.*=+$", re.M)


def leg_spread(legs: dict[str, str]) -> str:
    """Each pytest leg's wall in one CI run, raw and divided by its `leg_speed`.

    Args:
        legs: each leg's job name mapped to its raw log, as `green_pytest_legs` returns them.

    Returns:
        A first line holding both slowest-minus-fastest gaps, then one line per leg. Dividing
        by `leg_speed` takes out the hardware a leg drew, so the normalised gap is what the
        split itself left unbalanced (#7718).

    Raises:
        ValueError: `legs` is empty, or a leg's log holds no pytest summary line to read.
    """
    if not legs:
        raise ValueError(
            "no green pytest leg in this run, so there is no spread to read"
        )
    rows = []
    for name, text in sorted(legs.items()):
        clean = _LOG_NOISE.sub("", text)
        walls = _SUMMARY_WALL.findall(clean)
        if not walls:
            raise ValueError(f"{name}: no pytest summary line, so the leg has no wall")
        wall, speed = float(walls[-1]), leg_speed(clean)
        rows.append((name, wall, speed, wall / speed))
    raw = [r[1] for r in rows]
    normalised = [r[3] for r in rows]
    head = (
        f"{len(rows)} pytest legs: normalised gap {max(normalised) - min(normalised):.1f}s, "
        f"raw gap {max(raw) - min(raw):.1f}s"
    )
    return "\n".join(
        [head]
        + [
            f"{name}: {wall:.2f}s wall, leg_speed {speed:.3f}, {norm:.2f}s normalised"
            for name, wall, speed, norm in rows
        ]
    )


def runner_weights(reports: list[str]) -> dict[str, float]:
    """Each module's median seconds across CI `--durations` reports, one report per shard log.

    A module appears in one shard per run, so with logs from three runs each module takes the
    median of its three measurements, each divided by its leg's `leg_speed`.
    """
    seen: dict[str, list[float]] = {}
    for text in reports:
        clean = _LOG_NOISE.sub("", text)
        speed = leg_speed(clean)
        for module, seconds in parse_durations(clean).items():
            seen.setdefault(module, []).append(seconds / speed)
    return {m: round(statistics.median(v), 2) for m, v in sorted(seen.items())}


def load_runner_weights(path: Path = RUNNER_WEIGHTS_PATH) -> dict[str, float]:
    if not path.exists():
        return {}
    return {k: float(v) for k, v in json.loads(path.read_text()).items()}


def host_only_readings(readings: dict[str, float]) -> dict[str, float]:
    """The local readings `LOCAL_READING_MAX_RATIO`x the runner table or more, as runner seconds.

    A module the runner table lacks has no CI seconds to judge against, so it is never included.
    """
    runner = load_runner_weights()
    return {
        module: runner[module]
        for module, reading in readings.items()
        if module in runner
        and over_recorded(reading, runner[module], LOCAL_READING_MAX_RATIO)
    }


def runner_backed_readings(readings: dict[str, float]) -> dict[str, float]:
    """The local readings `host_only_readings` leaves out.

    A line on stdout names each one dropped.
    """
    rejected = host_only_readings(readings)
    for module, seconds in rejected.items():
        print(
            f"kept the recorded weight of {module}: this host read {readings[module]:.2f}s, the "
            f"runner table holds {seconds:.2f}s, and a reading {LOCAL_READING_MAX_RATIO:g}x or "
            "more above it is a cost of this host alone"
        )
    return {m: r for m, r in readings.items() if m not in rejected}


def write_runner_weights(
    reports: list[str], keep: list[str], path: Path = RUNNER_WEIGHTS_PATH
) -> dict[str, float]:
    """Rewrite the runner table from `reports`, for the modules in `keep`; return it.

    A run on a pull request also measures the modules that PR adds, which may never land.
    """
    kept = set(keep)
    table = {m: s for m, s in runner_weights(reports).items() if m in kept}
    if not table:
        raise ValueError(
            "no durations line in any report, so there is nothing to record"
        )
    path.write_text(json.dumps(table, indent=1, sort_keys=True) + "\n")
    return table


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


def packing_weights(
    recorded: dict[str, float], runner: dict[str, float]
) -> dict[str, float]:
    """The weights `assign` packs: runner seconds where measured, else scaled recorded ones."""
    ratios = [
        runner[m] / recorded[m]
        for m in runner
        if recorded.get(m, 0.0) >= _RATIO_FLOOR_SECONDS
    ]
    scale = statistics.median(ratios) if ratios else 1.0
    return {**{m: w * scale for m, w in recorded.items()}, **runner}


def packing_table(recorded: dict[str, float]) -> dict[str, float]:
    """`packing_weights` over the committed runner table."""
    return packing_weights(recorded, load_runner_weights())


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


def green_pytest_legs(run_id: int, gh=gh_mod.gh) -> dict[str, str]:
    """The raw log of every green pytest leg in CI run `run_id`, keyed by its job name.

    Each call goes through `lib.gh`, the invoking user's authenticated CLI with a 60s bound.
    A run that skipped its matrix returns an empty dict.

    Raises:
        subprocess.CalledProcessError: any `gh` call failed. A missing log fails the whole
            read rather than leaving one shard's modules unmeasured.
    """
    view = gh("run", "view", str(run_id), "--json", "jobs", cwd=REPO)
    legs = {
        job["name"]: job["databaseId"]
        for job in json.loads(view.stdout)["jobs"]
        if job["name"].startswith(PYTEST_JOB_PREFIX) and job["conclusion"] == "success"
    }
    # The flag because ci.yml runs pytest with `--color yes`, and `gh api` refuses a body
    # carrying escape sequences without it. `_LOG_NOISE` strips the colours.
    return {
        name: gh(
            "api",
            "--allow-escape-sequences",
            f"repos/{{owner}}/{{repo}}/actions/jobs/{leg}/logs",
            cwd=REPO,
        ).stdout
        for name, leg in legs.items()
    }


def fetch_runner_logs(runs: int, gh=gh_mod.gh) -> list[str]:
    """The raw log of every pytest leg in the newest `runs` green master runs that ran them.

    The docs-refresh cron holds the git-tree lock while this runs. Three runs of six legs is at
    most 1 + `RUN_WINDOW` + 18 calls, against the authenticated 5000 an hour.

    Raises:
        RuntimeError: fewer than `runs` of the newest `RUN_WINDOW` green runs carried a pytest
            log. A median over fewer
            runs is one noisy reading per module (#3604), so nothing is written from it.
        subprocess.CalledProcessError: any `gh` call failed.
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
        legs = green_pytest_legs(run["databaseId"], gh)
        measured_runs += bool(legs)
        reports += legs.values()
    if measured_runs < runs:
        raise RuntimeError(
            f"found pytest logs in {measured_runs} of the newest {len(listed)} green "
            f"{CI_BRANCH} run(s) of {CI_WORKFLOW}, not the {runs} asked for"
        )
    return reports
