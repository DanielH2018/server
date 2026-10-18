# ansible/roles/setup/gitops_deploy/files/gitops_tick_metrics.py
"""The GitOps tick as Prometheus metrics: how each tick ended, how long it ran, how far behind.

The tick kept four records before #5676 (`last_run`, the per-SHA ledger, Discord and the
journal) and none was a metric, so a run of lock-contention skips or parks showed only as
journal lines someone had to go looking for. Each tick now rewrites one node-exporter textfile:

    gitops_tick_runs_total{outcome}          a counter per outcome in `OUTCOMES`
    gitops_tick_last_duration_seconds        wall clock of the last tick the script ran
    gitops_tick_last_timestamp_seconds       when that file was last written
    gitops_tick_behind_seconds               seconds without a fast-forward while behind

`gitops_tick_behind_seconds` is `now - first_seen` from the `behind_since` marker, and 0 when
the host ended the tick at origin. It is NOT the age of the oldest unapplied commit: the
marker re-stamps on every fast-forward, so a deployer that keeps landing green ancestors of a
pending tip reads near 0 while staying behind the tip. It measures the thing the
behind-origin watchdog pages on, a deployer that stopped moving.

The counters live in the textfile itself: each write reads the previous counts back, adds
one, and writes every outcome, zeros included, so `increase()` sees a series from its first
tick rather than losing the first increment. A missing or unreadable file starts from zero,
which Prometheus reads as a counter reset.

Two writers. `gitops_deploy.entrypoint()` records each tick the script ran. A tick that never
reached Python, because `flock` gave up on the git-tree lock and exited 75, is recorded by the
unit's second `ExecStopPost=`, which runs this file as a script with `skipped-lock`. That
write keeps the previous duration, since no tick ran.

Stdlib only plus `gitops_markers`, because the `ExecStopPost=` invocation runs it outside the
deployer's import of `deployer_lib`.

Typical usage example:

    record(TEXTFILE, "merged", now=time.time(), duration=12.5, behind_marker=None)
"""

import argparse
import os
import re
import sys
import tempfile
import time

from gitops_markers import MARKERS, STATE_DIR, parse_behind

# The node-exporter textfile collector reads every `*.prom` file here.
TEXTFILE = "/var/lib/node-exporter-textfile/gitops_tick.prom"

# How a tick ended. Every one is written on every write, zeros included.
OUTCOMES = (
    # The tick fast-forwarded and handed the range to a plane: an apply, a flux-owned skip,
    # a deferral and a docs-only merge all read here.
    "merged",
    # Local was already at origin, or the tick re-rendered the denylist and stopped.
    "nothing",
    # Origin's CI had not finished and no green ancestor was ahead of local.
    "ci-wait",
    "ci-failed",
    # Origin is the SHA `hold_sha` names.
    "held",
    # A bring-up playbook or an unroutable setup role parked the range unmerged.
    "parked",
    # The working tree had uncommitted changes.
    "dirty",
    # A deploy deferred on a busy per-service lock, which `contention_since` records.
    "lock-deferred",
    # A crash, a config error, a non-zero exit, or a newly written `hold_sha`.
    "failed",
    # `git status` or `git fetch` failed in a way the next tick retries.
    "fetch-failed",
    # `flock` waited out its timeout on the git-tree lock; the script never ran.
    "skipped-lock",
)

_RUNS = re.compile(r'^gitops_tick_runs_total\{outcome="([^"]+)"\} (\d+)$')
_DURATION = re.compile(r"^gitops_tick_last_duration_seconds (\S+)$")


def read_previous(path: str) -> tuple[dict[str, int], float | None]:
    """The counts and the last duration the file at `path` holds.

    Returns:
        `(counts, duration)`. A missing or unreadable file, or a line this cannot parse,
        contributes nothing, so the counts start from zero and the duration is None.
    """
    counts: dict[str, int] = {}
    duration: float | None = None
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return counts, duration
    for line in lines:
        if m := _RUNS.match(line):
            counts[m.group(1)] = int(m.group(2))
        elif m := _DURATION.match(line):
            try:
                duration = float(m.group(1))
            except ValueError:
                duration = None
    return counts, duration


def behind_seconds(marker: str | None, now: float) -> float:
    """Seconds since the `behind_since` marker's first-seen stamp, or 0 with no marker."""
    parsed = parse_behind(marker)
    if parsed is None:
        return 0.0
    return max(0.0, now - parsed[1])


def render(
    counts: dict[str, int], duration: float | None, behind: float, now: float
) -> str:
    """The textfile body: every outcome's count, then the three gauges.

    A None `duration` leaves its gauge out, which is the state before the first tick ran.
    """
    lines = [
        "# HELP gitops_tick_runs_total GitOps deploy ticks, by how each ended.",
        "# TYPE gitops_tick_runs_total counter",
    ]
    lines += [
        f'gitops_tick_runs_total{{outcome="{o}"}} {counts.get(o, 0)}' for o in OUTCOMES
    ]
    if duration is not None:
        lines += [
            "# HELP gitops_tick_last_duration_seconds Wall clock of the last tick the script ran.",
            "# TYPE gitops_tick_last_duration_seconds gauge",
            f"gitops_tick_last_duration_seconds {duration:.3f}",
        ]
    lines += [
        "# HELP gitops_tick_behind_seconds Seconds without a fast-forward while behind origin; 0 at origin.",
        "# TYPE gitops_tick_behind_seconds gauge",
        f"gitops_tick_behind_seconds {behind:.0f}",
        "# HELP gitops_tick_last_timestamp_seconds When a tick last wrote this file.",
        "# TYPE gitops_tick_last_timestamp_seconds gauge",
        f"gitops_tick_last_timestamp_seconds {now:.0f}",
    ]
    return "\n".join(lines) + "\n"


def record(
    path: str,
    outcome: str,
    *,
    now: float,
    duration: float | None = None,
    behind_marker: str | None = None,
) -> None:
    """Add one tick ending in `outcome` to the textfile at `path`, atomically.

    Args:
        path: the `.prom` file to rewrite.
        outcome: one of `OUTCOMES`.
        now: the current time, in `time.time()` terms.
        duration: the tick's wall clock in seconds, or None to keep the previous value.
        behind_marker: the `behind_since` marker's contents, or None when it is absent.

    Raises:
        ValueError: `outcome` is not one of `OUTCOMES`.
        OSError: the directory cannot be written.
    """
    if outcome not in OUTCOMES:
        raise ValueError(
            f"unknown tick outcome {outcome!r}; expected one of {OUTCOMES}"
        )
    counts, previous = read_previous(path)
    counts[outcome] = counts.get(outcome, 0) + 1
    body = render(
        counts,
        previous if duration is None else duration,
        behind_seconds(behind_marker, now),
        now,
    )
    # The temp name does not end in `.prom`, so the collector never reads a half-written file.
    fd, tmp = tempfile.mkstemp(
        dir=os.path.dirname(path), prefix=".gitops_tick.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def main(argv: list[str] | None = None) -> int:
    """Record one tick from outside the deployer, which the unit's `ExecStopPost=` does."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("outcome", choices=OUTCOMES)
    parser.add_argument("--textfile", default=TEXTFILE)
    parser.add_argument("--state-dir", default=STATE_DIR)
    args = parser.parse_args(argv)
    try:
        with open(
            os.path.join(args.state_dir, MARKERS["behind"]), encoding="utf-8"
        ) as f:
            marker: str | None = f.read().strip() or None
    except OSError:
        marker = None
    record(args.textfile, args.outcome, now=time.time(), behind_marker=marker)
    return 0


if __name__ == "__main__":
    sys.exit(main())
