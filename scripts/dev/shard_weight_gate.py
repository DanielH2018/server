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

from lib import host_modules  # noqa: F401
from ci_verdict import WorkflowRun

_DURATION_LINE = re.compile(
    r"^([0-9]+(?:\.[0-9]+)?)s\s+(call|setup|teardown)\s+(\S+?)::"
)

# Only a CI measurement shows a heavy module's cost; below ten runner seconds the error is less than
# the hardware difference between legs (`leg_speed`).
RUNNER_HEAVY_SECONDS = 10.0

# Ratio 5 sits above the ~3x run-to-run and leg variance; the floor skips entries too small to
# decide the pole.
STALE_WEIGHT_RATIO = 5.0
STALE_WEIGHT_FLOOR_SECONDS = 3.0

# A module that grew after its weight was recorded packs as free; 20x clears run-to-run and hardware
# variance.
UNDER_WEIGHT_RATIO = 20.0

# Below the unweighted arm's 10s so entries recorded near-free but measuring 5s or more are
# annotated.
UNDER_WEIGHT_WARN_SECONDS = 5.0

# pytest's `--durations-min`: no measured total is smaller, so it is a divisor floor that cannot be
# zero.
_DURATIONS_MIN_SECONDS = 0.005

# A pull request cannot repair a weight: only green master runs write the table.
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


# Packed on runner seconds: the runner measures modules 1.0x to 5.5x a workstation's.
# Each leg's readings are scaled by its cold bytecode-compile speed (leg hardware varies).
_COMPILE_LINE = re.compile(
    r"Bytecode compiled (\d+) files in ([0-9]+(?:\.[0-9]+)?)(ms|s)\b"
)
# Fixed, so the table's scale does not move with the runs a refresh reads.
REFERENCE_COMPILE_SECONDS_PER_FILE = 0.00085
# A compile faster than this found its bytecode already built, so it measures no CPU.
_COLD_COMPILE_FLOOR_SECONDS_PER_FILE = 0.0002
RUNNER_WEIGHTS_PATH = (
    Path(__file__).resolve().with_name("pytest_shard_runner_weights.json")
)
# A measurement within this ratio of the recorded seconds is noise.
DRIFT_RATIO = 1.5
# An entry within this many seconds of its measurement stays, whatever the ratio.
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


# Logs come from green master pushes; a push that skipped its legs is read through the PR run that
# tested its tree (`_tested_run`). Other PR runs may measure modules that never land.
CI_WORKFLOW = "ci.yml"
CI_BRANCH = "master"
# A prefix, not a name list, so changing the matrix needs no edit here.
PYTEST_JOB_PREFIX = "pytest (shard "
# A merge selecting no test module measured nothing, and one landing batch's run stands behind up to
# four master commits.
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
    # ci.yml runs pytest with `--color yes`, and `gh api` refuses a body with escape sequences
    # without the flag.
    return {
        name: gh(
            "api",
            "--allow-escape-sequences",
            f"repos/{{owner}}/{{repo}}/actions/jobs/{leg}/logs",
            cwd=REPO,
        ).stdout
        for name, leg in legs.items()
    }


def _tested_run(run: WorkflowRun, gh) -> int | None:
    """The id of the run that tested master run `run`'s tree before it merged, or None.

    A push to master runs no pytest leg when a pull-request run already tested its tree, so
    that run's logs are the tree's measurement. `tested_tree.tested_run` finds it from the
    record the push itself was proven by.
    """
    url, _ = tested_tree.tested_run(
        lambda path: json.loads(gh("api", path, cwd=REPO).stdout),
        run.repository,
        run.head_sha,
        run.head_tree,
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
    listed = [
        WorkflowRun.parse(run)
        for run in json.loads(gh("api", listing, cwd=REPO).stdout)["workflow_runs"]
    ]
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
        logs = measured(run.id) or measured(_tested_run(run, gh))
        if logs:
            reports.append(logs)
    if len(reports) < runs:
        raise RuntimeError(
            f"read pytest durations from {len(reports)} run(s) behind the newest "
            f"{len(listed)} green {CI_BRANCH} run(s) of {CI_WORKFLOW}, not the {runs} asked for"
        )
    return [log for logs in reports for log in logs]
