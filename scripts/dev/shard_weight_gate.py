"""The measured shard-weight gate: what CI's `pytest_shard.py --check-durations` step decides.

It reads the pytest `--durations` report a CI shard already wrote, and complains about three
shapes no static check can see: a heavy test module with no recorded weight, a recorded weight
far above what its module measured, and a heavy module whose recorded weight is far below it.
`pytest_shard.py`'s module docstring has why the split needs the table and why the gate is a CI
step. This module holds the bounds and the three arms; `pytest_shard.main` reads the report,
hands over the committed table and turns the complaints into an exit code.

Split out of `pytest_shard.py` when the margin band (#4010) took that module past the
600-line cap.
"""

import re

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

# What an unweighted module may measure before the gate FAILS the run rather than annotating it.
#
# WHY A MARGIN OVER THE BOUND. One wall-clock reading against a fixed 10s bound is a coin flip
# for a module near it: `scripts/dev/tests/test_fanout_red_gate.py` measured 12.4s on one PR run
# and 11.2s on its rerun, and the step failed 6 branches in 10 runs that way (#4010). A module in
# that band skews a ~40s shard by a quarter at most, and the docs-refresh cron records it
# unattended within a day, so the band annotates and only a module past it fails. The band
# narrows the flake rather than removing it: #3604 saw one module measure 8s and 15.6s on two
# runs of the same code, so a module near 15s can still land either side of it. 15s is still
# well under the ~30s module that motivated the gate.
RUNNER_HEAVY_FAIL_SECONDS = 15.0

# How many times its measured runner seconds a RECORDED weight may claim before the shard gate
# rejects it, and the recorded seconds below which nobody cares.
#
# WHY AN OVER-RECORDING ARM. The unweighted arm asks "is this file missing from the
# table". It never asks "is a recorded number still roughly what the file costs", so an entry that
# has become far too high stands until someone runs a full `--record`: `--record-missing` only
# fills gaps. A recorded 17.31s for `ansible/tests/k8s/test_secret_consumer_census.py` that
# measured 0.09s made the greedy split place it first, so one shard carried 17s of phantom
# weight for as long as the entry stood — the skew of an unweighted heavy file with the sign
# flipped.
#
# WHY A RATIO AND NOT A DELTA. The table holds workstation seconds and the report holds runner
# seconds, and the two are not the same scale. Controls measured on daniel-server
# ran 1.10x to 1.46x their recorded values, so anything under about 3x is that scale
# difference rather than a stale entry. FIVE leaves room above that noise, and a bound set
# nearer the noise would turn into a weekly chore. Five is where the arm ANNOTATES; it fails
# only at `STALE_WEIGHT_FAIL_RATIO`, because a busy recording host overshoots five on its own.
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

# The ratio at which an over-recorded weight FAILS the run. Between `STALE_WEIGHT_RATIO` and
# this it annotates, as the under-recorded arm's band below does.
#
# WHY TWENTY. A weight recorded on a host busy with fan-out batches comes out up to about nine
# times what the runner measures, and FIVE failed three PRs on such entries (#5616):
# `test_fanout_clean_chain.py` recorded 5.23s and measured 0.99s (5.3x), and a re-record on the
# same busy host gave 9.14s (9.2x); `test_media_volume_verify.py` recorded 4.86s and measured
# 0.97s (5.0x); `test_session_health.py` recorded 4.00s and measured 0.77s (5.2x). Each red
# cost a CI round and a hand edit to the table. A module recorded 5x to 9x high costs a few
# seconds of balance in a shard that projects around 40s, which is what this arm is about.
# `UNDER_WEIGHT_RATIO` uses twenty for the same runner-versus-workstation variance, and the
# 190x entry this arm exists for still fails. An entry in the band stays visible as an
# annotation, and the docs-refresh cron's `--refresh-drifted` rewrites it.
STALE_WEIGHT_FAIL_RATIO = 20.0

# How many times its recorded weight a module may measure on the runner before the shard gate
# reads the entry as too LOW. The arm fails the run at the unweighted arm's bound and annotates
# from `UNDER_WEIGHT_WARN_SECONDS` below.
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
# first reader, which `ansible/tests/conftest.py` now builds outside every timed phase; this
# band is what makes that shape visible again if the build ever lands back on a test. Between
# here and `RUNNER_HEAVY_SECONDS` a 20x ratio needs a weight of 0.25-0.5s or less, so the band
# annotates only near-free entries, and it never fails the run: a 5s module is an eighth of a
# shard, the skew the margin band above already tolerates.
UNDER_WEIGHT_WARN_SECONDS = 5.0

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
    "--record-files <path>...` and commit scripts/dev/pytest_shard_weights.json; for a "
    "path in pytest_shard.py's CI_ONLY_WEIGHTS, set its pin to the measured seconds instead"
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
    `heavy_underweighted` takes a measured value FAR above it.
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
        f"shard split reserves time the file does not cost: {listed}. "
        f"Repair (seconds): {RECORD_FILES_HINT}"
    )


def _heavy_complaint(heavy: list[tuple[str, float]], threshold: float) -> str:
    listed = ", ".join(f"{f} ({s:.1f}s)" for f, s in heavy)
    return (
        f"measured {threshold:.0f}s or more on this runner with no recorded weight, so "
        f"the shard split packs it as the lightest thing in the suite: {listed}. "
        f"Repair (seconds): {RECORD_MISSING_HINT}"
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
        f"Repair (seconds): {RECORD_FILES_HINT}. If it records near 0s again, the time is a "
        "per-process cache its first test built: build that cache outside the timed phases, "
        "as ansible/tests/conftest.py does for the k8s render"
    )


def durations_warnings(
    text: str,
    weights: dict[str, float],
    threshold: float = RUNNER_HEAVY_SECONDS,
    fail_above: float = RUNNER_HEAVY_FAIL_SECONDS,
    under_ratio: float = UNDER_WEIGHT_RATIO,
    under_threshold: float = UNDER_WEIGHT_WARN_SECONDS,
    ratio: float = STALE_WEIGHT_RATIO,
    stale_fail_ratio: float = STALE_WEIGHT_FAIL_RATIO,
) -> list[str]:
    """The complaints about a durations report that annotate the run and do not fail it.

    An unweighted module measuring `threshold` or more but under `fail_above`: runner variance
    alone moves a module across `threshold` (see `RUNNER_HEAVY_FAIL_SECONDS`). An
    under-recorded module annotates from `under_threshold` instead (see
    `UNDER_WEIGHT_WARN_SECONDS`). An over-recorded entry annotates from `ratio` up to
    `stale_fail_ratio` (see `STALE_WEIGHT_FAIL_RATIO`). Empty when the report parses to
    nothing, which `durations_problems` already fails on.
    """
    warnings = []
    margin = [
        (f, s) for f, s in heavy_unweighted(text, weights, threshold) if s < fail_above
    ]
    if margin:
        warnings.append(_heavy_complaint(margin, threshold))
    under = [
        row
        for row in heavy_underweighted(text, weights, under_threshold, under_ratio)
        if row[2] < fail_above
    ]
    if under:
        warnings.append(_under_complaint(under, under_threshold, under_ratio))
    stale = [
        row
        for row in stale_overweight(text, weights, ratio)
        if row[1] < stale_fail_ratio * max(row[2], _DURATIONS_MIN_SECONDS)
    ]
    if stale:
        warnings.append(_stale_complaint(stale, ratio))
    return warnings


def durations_problems(
    text: str,
    weights: dict[str, float],
    threshold: float = RUNNER_HEAVY_SECONDS,
    ratio: float = STALE_WEIGHT_RATIO,
    fail_above: float = RUNNER_HEAVY_FAIL_SECONDS,
    under_ratio: float = UNDER_WEIGHT_RATIO,
    stale_fail_ratio: float = STALE_WEIGHT_FAIL_RATIO,
) -> list[str]:
    """What is wrong with a shard's durations report, as readable complaints that fail the run.

    Three arms over the one report, and all are reported: a heavy module with NO recorded
    weight, a recorded weight far HIGHER than what the module measured here, and a heavy module
    whose recorded weight is far LOWER. Each complaint carries its own repair, because the
    repairs differ — `--record-missing` fills a gap and cannot refresh an entry that is merely
    wrong.

    It does not know which modules a pull request changed; `gate_complaints` splits the report
    by that before calling it.

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
    if under := heavy_underweighted(text, weights, fail_bound, under_ratio):
        problems.append(_under_complaint(under, fail_bound, under_ratio))
    stale_bound = max(ratio, stale_fail_ratio)
    if stale := stale_overweight(text, weights, stale_bound):
        problems.append(_stale_complaint(stale, stale_bound))
    return problems


# The prefix a complaint about a module outside a pull request's diff carries when it annotates.
OUTSIDE_DIFF_NOTE = (
    "outside this pull request's diff, so it annotates and does not fail; the docs-refresh "
    "cron's --record-missing and --refresh-drifted repair it"
)


# The files that decide every module's verdict at once: the weight table and the gate's code.
# A diff touching one can repair any module, so the gate runs unscoped on it.
GATE_INPUTS = frozenset(
    {
        "scripts/dev/pytest_shard_weights.json",
        "scripts/dev/pytest_shard.py",
        "scripts/dev/shard_weight_gate.py",
    }
)


def split_by_diff(text: str, changed: set[str]) -> tuple[str, str]:
    """The durations lines of the modules in `changed`, and the lines of every other module.

    Each half keeps every phase of its modules, so each module sums to the same total in its
    half as in the whole report. Lines that are not durations lines go in neither half.
    """
    inside, outside = [], []
    for line in text.splitlines():
        match = _DURATION_LINE.match(line.strip())
        if match:
            (inside if match.group(3) in changed else outside).append(line)
    return "\n".join(inside), "\n".join(outside)


def gate_complaints(
    text: str,
    weights: dict[str, float],
    changed: set[str] | None = None,
    threshold: float = RUNNER_HEAVY_SECONDS,
    ratio: float = STALE_WEIGHT_RATIO,
    fail_above: float = RUNNER_HEAVY_FAIL_SECONDS,
) -> tuple[list[str], list[str]]:
    """`(problems, warnings)` for a durations report: what fails the run, and what annotates it.

    `changed` is the set of paths a pull request's diff adds or modifies, or None for a run
    with no diff to scope to. With None, every module's complaint counts, as on a push. With a
    set, only a module in it can fail the run. Every other module's complaint becomes a
    warning, because its author is not the one reading this run: 4 of the 16 gate failures
    between 2026-10-15 and 2026-10-17 named a module outside the failing PR's diff (#5480).

    A set that holds one of `GATE_INPUTS` counts as None. A PR that re-records the table or
    moves a bound wrote every row the gate then reads, so every complaint is its author's.

    The empty-report complaint reads the WHOLE report. Most PRs change no test module a given
    shard ran, so a scoped half is usually empty, and that is not a format change.
    """
    if changed is None or changed & GATE_INPUTS or not parse_durations(text):
        return (
            durations_problems(text, weights, threshold, ratio, fail_above),
            durations_warnings(text, weights, threshold, fail_above, ratio=ratio),
        )
    inside, outside = split_by_diff(text, changed)
    problems, warnings = [], []
    if inside:
        problems = durations_problems(inside, weights, threshold, ratio, fail_above)
        warnings = durations_warnings(
            inside, weights, threshold, fail_above, ratio=ratio
        )
    if outside:
        warnings += [
            f"{OUTSIDE_DIFF_NOTE}: {complaint}"
            for complaint in durations_problems(
                outside, weights, threshold, ratio, fail_above
            )
            + durations_warnings(outside, weights, threshold, fail_above, ratio=ratio)
        ]
    return problems, warnings
