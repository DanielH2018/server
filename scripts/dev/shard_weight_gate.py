"""The measured shard-weight gate: what CI's `pytest_shard.py --check-durations` step decides.

It reads the pytest `--durations` report a CI shard already wrote, and complains about two
shapes no static check can see: a heavy test module with no recorded weight, and a recorded
weight far above what its module measured. `pytest_shard.py`'s module docstring has why the
split needs the table and why the gate is a CI step. This module holds the bounds and the two
arms; `pytest_shard.main` reads the report, hands over the committed table and turns the
complaints into an exit code.

Split out of `pytest_shard.py` when the margin band (#4010) took that module past the
600-line cap.
"""

import re

# A line of pytest's durations report: seconds, phase, then a nodeid whose leading segment,
# up to the first pair of colons, is the test file this weight belongs to.
_DURATION_LINE = re.compile(r"^([0-9.]+)s\s+(call|setup|teardown)\s+(\S+?)::")

# What an unweighted module may measure on the CI runner before the shard gate rejects it.
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

# What an unweighted module may measure before the gate FAILS the run rather than annotating it.
#
# WHY A MARGIN OVER THE BOUND. One wall-clock reading against a fixed 10s bound is a coin flip
# for a module near it: `scripts/dev/tests/test_fanout_red_gate.py` measured 12.4s on one PR run
# and 11.2s on its rerun, and the step failed 6 branches in 10 runs that way (#4010). A module in
# that band skews a ~40s shard by a quarter at most, and the docs-refresh cron records it
# unattended within a day, so the band annotates and only a module past it fails. 15s sits
# above the variance seen near the bound and still well under the ~30s module that motivated
# the gate.
RUNNER_HEAVY_FAIL_SECONDS = 15.0

# How many times its measured runner seconds a RECORDED weight may claim before the shard gate
# rejects it, and the recorded seconds below which nobody cares.
#
# WHY A SECOND ARM AT ALL. Every other arm of the gate asks "is this file missing from the
# table". None asks "is a recorded number still roughly what the file costs", so an entry that
# has become far too high stands until someone runs a full `--record`: `--record-missing` only
# fills gaps. A recorded 17.31s for `ansible/tests/k8s/test_secret_consumer_census.py` that
# measured 0.09s made the greedy split place it first, so one shard carried 17s of phantom
# weight for as long as the entry stood — the skew of an unweighted heavy file with the sign
# flipped.
#
# WHY A RATIO AND NOT A DELTA. The table holds workstation seconds and the report holds runner
# seconds, and the two are not the same scale. Controls measured on daniel-server
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
# invisible here. An entry that is merely far too high still produces lines, which is the
# shape that occurs in practice.
STALE_WEIGHT_RATIO = 5.0
STALE_WEIGHT_FLOOR_SECONDS = 3.0

# pytest's own `--durations-min`: no report line is smaller, so no measured total is either.
# Used as the divisor's floor, which cannot then be zero.
_DURATIONS_MIN_SECONDS = 0.005

# The repair for the gate arm that finds a MISSING entry. `census()` reads `git ls-files`, so
# the file has to be staged before the measurement can see it.
RECORD_MISSING_HINT = (
    "stage the new file, then `uv run python scripts/dev/pytest_shard.py "
    "--record-missing` and commit scripts/dev/pytest_shard_weights.json"
)

# The repair for the stale arm, which `--record-missing` cannot do: it only fills gaps, and a
# present-but-wrong entry is not a gap. A full `--record` re-measures the whole suite in
# minutes; `--record-files` re-measures the named files in seconds, so the gate has a repair
# cheap enough that nobody reaches for a stale entry as the lesser evil.
RECORD_FILES_HINT = (
    "re-measure just those files with `uv run python scripts/dev/pytest_shard.py "
    "--record-files <path>...` and commit scripts/dev/pytest_shard_weights.json"
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


def _heavy_complaint(heavy: list[tuple[str, float]], threshold: float) -> str:
    listed = ", ".join(f"{f} ({s:.1f}s)" for f, s in heavy)
    return (
        f"measured {threshold:.0f}s or more on this runner with no recorded weight, so "
        f"the shard split packs it as the lightest thing in the suite: {listed}. "
        f"Repair (seconds): {RECORD_MISSING_HINT}"
    )


def durations_warnings(
    text: str,
    weights: dict[str, float],
    threshold: float = RUNNER_HEAVY_SECONDS,
    fail_above: float = RUNNER_HEAVY_FAIL_SECONDS,
) -> list[str]:
    """The complaints about a durations report that annotate the run and do not fail it.

    An unweighted module measuring `threshold` or more but under `fail_above`: runner variance
    alone moves a module across `threshold` (see `RUNNER_HEAVY_FAIL_SECONDS`). Empty when the
    report parses to nothing, which `durations_problems` already fails on.
    """
    margin = [
        (f, s) for f, s in heavy_unweighted(text, weights, threshold) if s < fail_above
    ]
    return [_heavy_complaint(margin, threshold)] if margin else []


def durations_problems(
    text: str,
    weights: dict[str, float],
    threshold: float = RUNNER_HEAVY_SECONDS,
    ratio: float = STALE_WEIGHT_RATIO,
    fail_above: float = RUNNER_HEAVY_FAIL_SECONDS,
) -> list[str]:
    """What is wrong with a shard's durations report, as readable complaints that fail the run.

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
    problems = []
    fail_bound = max(threshold, fail_above)
    if heavy := heavy_unweighted(text, weights, fail_bound):
        problems.append(_heavy_complaint(heavy, fail_bound))
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
