"""The measured shard-weight check: what CI's `pytest_shard.py --check-durations` step reports.

It reads the pytest `--durations` report a CI shard already wrote, and complains about three
shapes no static check can see: a heavy test module with no recorded weight, a recorded weight
far above what its module measured, and a heavy module whose recorded weight is far below it.
`pytest_shard.py`'s module docstring has why the split needs the table and why no complaint
fails the run. This module holds the bounds and the three arms; `pytest_shard.main` reads the
report, hands over the committed table and prints each complaint as an annotation.

Split out of `pytest_shard.py` when the margin band took that module past the
600-line cap. The runner-seconds table that `pytest_shard.py` packs the shards on lives here
too, at the end, for the same reason, with the fetch of CI's pytest leg logs that both
the table and `leg_spread` read.
"""

import json
import re
import statistics
from pathlib import Path

from dev import tested_tree
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
# `def test_` count, byte size and directory mean each fail to separate a heavy module from a
# light one.
# Only running the file says what it costs, and CI already runs it.
#
# WHY TEN. The table and the report both hold runner seconds, and an unweighted module is packed
# at its directory's median whatever it costs. Ten measured seconds is the weight the split
# placed by that guess. A lighter module costs less balance than the hardware two legs draw
# differs by (`leg_speed`).
RUNNER_HEAVY_SECONDS = 10.0

# How many times its measured runner seconds a RECORDED weight may claim before the shard check
# annotates it, and the recorded seconds below which nobody cares.
#
# WHY AN OVER-RECORDING ARM. The unweighted arm asks "is this file missing from the
# table". It never asks "is a recorded number still roughly what the file costs", so an entry
# that has become far too high stands until `--refresh-runner` reads the module off a green
# master run. A recorded 17.31s for `tests/k8s/test_secret_consumer_census.py` that
# measured 0.09s made the greedy split place it first, so one shard carried 17s of phantom
# weight for as long as the entry stood — the skew of an unweighted heavy file with the sign
# flipped.
#
# WHY A RATIO AND NOT A DELTA. One leg's hardware runs a shard 0.67x to 1.22x its table
# (`leg_speed`), and one module can measure 2x apart on two runs of the same code, so anything
# under about 3x is that variance rather than a stale entry. FIVE leaves room above it.
#
# WHY A FLOOR ON THE RECORDED VALUE. The parser's own floor is pytest's 0.005s, so a file
# recorded at 0.05s and measuring 0.005s is a 10x ratio and pure rounding. The floor is what
# makes this arm about shard balance rather than about noise: only the files heavy enough for
# their placement to decide the pole are compared.
#
# THE BLIND SPOT. A file whose every test measures under pytest's `--durations-min` contributes
# no line and cannot be compared at all, so an entry that collapsed to literally nothing is
# invisible here. An entry that is merely far too high still produces lines, which is the
# shape that occurs in practice.
STALE_WEIGHT_RATIO = 5.0
STALE_WEIGHT_FLOOR_SECONDS = 3.0

# How many times its recorded weight a module may measure on the runner before the shard check
# reads the entry as too LOW. The arm annotates from `UNDER_WEIGHT_WARN_SECONDS` below.
#
# WHY A THIRD ARM. The stale arm above catches a recorded number far too high. A module that
# grew after its weight was recorded is packed as free until `--refresh-runner` reads it again:
# the skew of an unweighted heavy file, with an entry present.
#
# WHY TWENTY. One module can vary 2x between two runs of the same code, and the hardware a leg
# draws adds its own factor. Twenty clears both. The entries this arm exists for are 34x to
# 1500x off.
UNDER_WEIGHT_RATIO = 20.0

# Where the under-recorded arm starts ANNOTATING, below the 10s the unweighted arm uses.
#
# WHY LOWER. The bar is "no module recorded under 1s totals over 5s", and a module measuring
# 7.4-7.7s against 0.0-0.01s recorded sits under the 10s bound, so the arm could not see it.
# That shape is the k8s render charged to its first reader, which `tests/conftest.py` builds
# outside every timed phase; this band makes the shape visible again if the build ever lands
# back on a test. Between
# here and `RUNNER_HEAVY_SECONDS` a 20x ratio needs a weight of 0.25-0.5s or less, so the band
# annotates only near-free entries.
UNDER_WEIGHT_WARN_SECONDS = 5.0

# pytest's own `--durations-min`: no report line is smaller, so no measured total is either.
# Used as the divisor's floor, which cannot then be zero.
_DURATIONS_MIN_SECONDS = 0.005

# What every complaint ends on. A pull request cannot repair a weight: only green master runs
# write the table.
REPAIR = (
    "Nothing to repair in this pull request: the docs-refresh cron's `pytest_shard.py "
    "--refresh-runner` reads the module off green master runs within a day of the merge"
)


def parse_durations(text: str) -> dict[str, float]:
    """Per-file seconds summed out of a pytest `--durations` report.

    One parser for both readers: `runner_weights`, which writes the table from CI's leg logs,
    and `heavy_unweighted` below, which checks one leg's log against it. A second parser would
    drift from the first and the drift would read green.

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

    Over-recording only: `heavy_underweighted` takes a measured value far above its recorded one.
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


def _stale_complaint(stale: list[tuple[str, float, float]], ratio: float) -> str:
    listed = ", ".join(
        f"{f} (recorded {rec:.2f}s, measured {got:.2f}s)" for f, rec, got in stale
    )
    return (
        f"recorded at {ratio:.0f}x or more of what it measured on this runner, so the "
        f"shard split reserves time the file does not cost: {listed}. {REPAIR}"
    )


def _heavy_complaint(heavy: list[tuple[str, float]], threshold: float) -> str:
    listed = ", ".join(f"{f} ({s:.1f}s)" for f, s in heavy)
    return (
        f"measured {threshold:.0f}s or more on this runner with no recorded weight, so "
        f"the shard split packs it at its directory's median: {listed}. {REPAIR}"
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
        f"{REPAIR}. If its weight stays near 0s, the time is a "
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
    whose recorded weight is far LOWER. Each complaint ends on `REPAIR`.

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


# ── The runner-seconds table the shards are packed on ─────────────────────────────────
#
# WHY THE RUNNER'S SECONDS AND NOT A WORKSTATION'S. The runner does not scale a workstation's
# serial seconds evenly. The middle 90% of the modules a workstation measured at 0.5s or more
# measure 1.0x to 5.5x that on the runner, with a median of 2.3x, so shards packed at 157-159
# workstation seconds each measured 327-612s there. Packing on what the runner measures
# projects every shard at about the same runner seconds.
#
# STALENESS IS GRACEFUL. A module this table lacks is packed at the median of its directory's
# rows (`pytest_shard.packing_seconds`), and an entry for a deleted module
# is ignored. The docs-refresh cron refreshes it twice a day with `pytest_shard.py
# --refresh-runner 3`, which downloads every pytest leg's log from the three newest runs that
# tested a green master tree, and rewrites only the rows that drifted. Each module takes the
# median of what the logs measured, because one module can measure about 8s on one run and
# 15.6s on the next.
# `--record-runner LOG...` is the by-hand full rewrite from logs you downloaded yourself.
#
# EACH LEG'S READINGS ARE SCALED TO ONE REFERENCE RUNNER FIRST, because the hardware a leg draws
# sets the speed of every module in it. One unchanged shard can run 0.67x its table on one leg
# and 1.22x on the next, and the six legs of one run can spread 140s to 290s on a split
# projected flat. That spread is wider than `DRIFT_RATIO`, and every module in a shard shares
# it, so the medians of several runs move many rows on each refresh, correlated by shard. The
# leg's own cold `uv sync` bytecode compile measures the same hardware before any test runs.
# Its seconds per file predicts the leg's factor within about 8%, and scaling by it cuts a
# module's spread across runs.
_COMPILE_LINE = re.compile(r"Bytecode compiled (\d+) files in ([0-9.]+)(ms|s)\b")
# The cold compile's seconds per file on a leg that measures its modules at their table weight.
# A fixed constant, so the table's scale does not move with the runs a refresh happens to read.
REFERENCE_COMPILE_SECONDS_PER_FILE = 0.00085
# A compile faster than this per file found its bytecode already built, so it measures no CPU.
# Cold compiles measured 0.53-1.0 ms per file and warm ones 0.03-0.06 ms.
_COLD_COMPILE_FLOOR_SECONDS_PER_FILE = 0.0002
RUNNER_WEIGHTS_PATH = (
    Path(__file__).resolve().with_name("pytest_shard_runner_weights.json")
)
# How far apart a module's recorded and measured seconds may sit, either way, before
# `--refresh-runner` rewrites the entry. A measurement inside it is treated as noise.
DRIFT_RATIO = 1.5
# An entry within this many seconds of what it measured stays, whatever the ratio: 0.02s
# measuring 0.05s is 2.5x and changes no shard's balance.
DRIFT_FLOOR_SECONDS = 0.5

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
        split itself left unbalanced.

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


# Where `--refresh-runner` reads its logs: the trees of the green pushes to master. A push
# that skipped its legs is read through the pull-request run that tested its tree
# (`_tested_run`). Any other pull request's run measures modules that may never land, and a
# merge-queue run repeats a commit master runs again, so neither counts.
CI_WORKFLOW = "ci.yml"
CI_BRANCH = "master"
# Every pytest leg is named `pytest (shard N)` in ci.yml. A prefix rather than a name list, so
# changing the matrix needs no edit here.
PYTEST_JOB_PREFIX = "pytest (shard "
# How many green master runs `fetch_runner_logs` looks behind for the runs that measured their
# trees. A merge whose diff selects no test module measured nothing, and one landing batch's
# run stands behind up to four master commits, so the newest few do not always yield three.
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


def _tested_run(run: dict, gh) -> int | None:
    """The id of the run that tested master run `run`'s tree before it merged, or None.

    A push to master runs no pytest leg when a pull-request run already tested its tree, so
    that run's logs are the tree's measurement. `tested_tree.tested_run` finds it from the
    record the push itself was proven by. `run` is one row of GitHub's workflow-runs listing.
    """
    url, _ = tested_tree.tested_run(
        lambda path: json.loads(gh("api", path, cwd=REPO).stdout),
        run["repository"]["full_name"],
        run["head_sha"],
        (run.get("head_commit") or {}).get("tree_id", ""),
    )
    return int(url.rsplit("/", 1)[1]) if url else None


def fetch_runner_logs(runs: int, gh=gh_mod.gh) -> list[str]:
    """The raw pytest leg logs of the newest `runs` runs that measured a green master tree.

    Each green master run is read through its own pytest legs, or through the run
    `_tested_run` names when it has none. A run counts once, however many master commits it
    tested, and only when one of its logs holds a durations line.

    The docs-refresh cron runs this twice a day. A master run costs up to four calls to look
    at and six more to read, so three runs found at once is about 30 calls and a whole
    `RUN_WINDOW` of unmeasured runs is 121, against the authenticated 5000 an hour.

    Raises:
        RuntimeError: fewer than `runs` runs measured the newest `RUN_WINDOW` green master
            trees. A median over fewer
            runs is one noisy reading per module, so nothing is written from it.
        subprocess.CalledProcessError: any `gh` call failed.
    """
    listing = (
        f"repos/{{owner}}/{{repo}}/actions/workflows/{CI_WORKFLOW}/runs"
        f"?branch={CI_BRANCH}&event=push&status=success&per_page={RUN_WINDOW}"
    )
    listed = json.loads(gh("api", listing, cwd=REPO).stdout)["workflow_runs"]
    seen: set[int] = set()

    def measured(run_id: int | None) -> list[str]:
        if run_id is None or run_id in seen:
            return []
        seen.add(run_id)
        logs = list(green_pytest_legs(run_id, gh).values())
        with_durations = any(parse_durations(_LOG_NOISE.sub("", log)) for log in logs)
        return logs if with_durations else []

    reports: list[list[str]] = []
    for run in listed:
        if len(reports) == runs:
            break
        logs = measured(run["id"]) or measured(_tested_run(run, gh))
        if logs:
            reports.append(logs)
    if len(reports) < runs:
        raise RuntimeError(
            f"read pytest durations from {len(reports)} run(s) behind the newest "
            f"{len(listed)} green {CI_BRANCH} run(s) of {CI_WORKFLOW}, not the {runs} asked for"
        )
    return [log for logs in reports for log in logs]
