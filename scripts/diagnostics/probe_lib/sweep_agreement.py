"""`probe.py sweep-agreement` — whether daniel-box's shadow sweep agrees with hosted CI.

Slice B1c of docs/ci-throughput.md. `homelab-local-sweep.service` sweeps the tip of master and
prints one `SWEEP-RESULT:` line per commit, which nothing reads. Slice B2 lets a landing trust
that verdict, and it may start only when a week of sweeps agrees with the hosted runs of the
same commits. This probe makes the comparison. It reads:

- the sweep's journal lines from Loki, over `--since`;
- GitHub's check-runs of each swept commit, for the gate jobs the sweep runs. That is one
  GitHub read per swept commit, about 300 for a full window, so do not run it in a loop.

A sweep that `local_sweep.py --run <id>` made replays what one pull-request run tested, and
its line carries `run=<id>`. The probe compares it with that run's own jobs. These sweeps are
where the hosted reds come from, because master's runs are nearly all green. A commit or a run
that was swept twice counts once, by its newest sweep.

A hosted job that skipped beside a green gate counts as green. The gate passes a skipped job
only when `prove` showed that a pull-request run had already tested the same tree. Most master
commits are proven this way, and a proven tree is green by construction. The count line
therefore says how many checks hosted CI ran itself and how many of all the hosted verdicts
were red, because only a hosted red gives the sweep a chance to miss one.

It prints a count line, each disagreement with the commit and the job, and where B2's gate
stands. A sweep that read red where hosted CI read green is a good pull request a queue would
have refused, and it is listed with what the run printed. A sweep that read green where hosted
CI read red is a red tree a queue would have merged.

Exit 1 when a sweep read green where hosted CI read red. 3 when the window holds no sweep. 75
when Loki or GitHub could not be read, or the window holds more lines than one Loki query
returns.
"""

# `probe_lib` is a namespace package under `scripts/`, so reaching a sibling by package name
# needs `scripts/` on sys.path — a module gets only its importer's path otherwise, and
# pyproject's `pythonpath` is a pytest setting. This has to sit ABOVE the imports below.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import json
import re
import subprocess
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from diagnostics.probe_lib import core
from diagnostics.probe_lib.obs_api import loki_query_url, trailing_window_ns
from lib.exit_codes import PROBE_FAILED, PROBE_NOT_APPLICABLE, PROBE_OK
from lib.gh import gh
from lib.repo_paths import HOST_LIB_FILES, REPO

# `local_sweep` owns the result line, and `ci_verdict` the reading of one check-run. Neither
# directory is on a directly-invoked probe.py's sys.path.
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2] / "deploy_tools"))
_sys.path.insert(0, str(HOST_LIB_FILES))

from ci_verdict import CI_GATE_NAME, _run_state
from local_sweep import Sweep, parse_result_line

# Every line the unit logs. Alloy labels a syslog line with its tag, the unit's
# SyslogIdentifier=.
LOGQL = '{job="syslog", program="local-sweep"}'
_LINE = re.compile(r"local-sweep\[(\d+)\]: (.*)$")
# Loki's own cap on one query, across every stream. A window that fills it has lost sweeps.
LIMIT = 5000
# A line this much older than a result line is another run's, under a reused pid. It is longer
# than the unit's TimeoutStartSec=.
_RUN_SECONDS = 2 * 3600

# The check-run names of each gate job the sweep runs start with these.
HOSTED_JOBS = {"hooks": "hooks (", "pytest": "pytest ("}

# B2's gate, from docs/ci-throughput.md.
GATE_DAYS = 7
GATE_SWEEPS = 300
GATE_P90_SECONDS = 600
GATE_HOSTED_REDS = 20

# `probe.py sweep-agreement`'s help as `cli_parser._verdict_help` takes it.
SWEEP_AGREEMENT_HELP = (
    "the shadow sweep on daniel-box against hosted CI: each swept commit's verdict beside the "
    "hosted run's, the disagreements, and where slice B2's gate stands",
    "no sweep read green where hosted CI read red",
    "one did",
)

Rows = list[tuple[int, str]]
CheckRuns = list[dict]


@dataclass(frozen=True)
class Swept:
    """One sweep as the journal recorded it.

    Attributes:
        at: When it ended, in seconds since the epoch.
        sweep: The result line, parsed.
        printed: What the run printed above the result line.
    """

    at: float
    sweep: Sweep
    printed: tuple[str, ...]

    @property
    def hosted_at(self) -> str:
        """The GitHub API path, under the repository, of the verdicts this sweep is set beside."""
        if self.sweep.run:
            return f"actions/runs/{self.sweep.run}/jobs"
        return f"commits/{self.sweep.sha}/check-runs"


def swept(rows: Rows) -> list[Swept]:
    """The newest sweep of each commit or run in Loki's rows, with the lines its run printed.

    A run's lines share its pid. A run that died printed no result line, and its lines stay
    with its pid and reach no sweep.
    """
    printed: dict[str, list[tuple[int, str]]] = {}
    found: dict[str, Swept] = {}
    for at_ns, line in rows:
        match = _LINE.search(line)
        if not match:
            continue
        pid, message = match.groups()
        sweep = parse_result_line(message)
        if sweep is None:
            printed.setdefault(pid, []).append((at_ns, message))
            continue
        own = (m for at, m in printed.pop(pid, ()) if at_ns - at < _RUN_SECONDS * 1e9)
        one = Swept(at_ns / 1e9, sweep, tuple(own))
        found.pop(one.hosted_at, None)
        found[one.hosted_at] = one
    return list(found.values())


def hosted(check_runs: CheckRuns) -> dict[str, str]:
    """What hosted CI said of each job of `HOSTED_JOBS`, from a commit's check-runs or a run's jobs.

    Returns:
        Per job: `pass` or `fail` for a job that ran, `proven` for one that skipped beside a
        gate that passed, and `none` for a job with no run, one still running, and one that
        was cancelled. A re-run job has several check-runs under one name, and the newest
        decides. A leg that ran outranks a skipped one, so a commit with both a proven run and
        a full run reads by the full run.
    """
    newest: dict[str, dict] = {}
    for run in check_runs:
        seen = newest.get(run["name"])
        if seen is None or run["id"] > seen["id"]:
            newest[run["name"]] = run
    gate_passed = _run_state(newest.get(CI_GATE_NAME, {})) == "pass"
    verdicts = {}
    for job, prefix in HOSTED_JOBS.items():
        states = {
            _run_state(r) for name, r in newest.items() if name.startswith(prefix)
        }
        ran = states - {"skipped"}
        if "fail" in ran:
            verdicts[job] = "fail"
        elif ran == {"pass"}:
            verdicts[job] = "pass"
        elif states == {"skipped"} and gate_passed:
            verdicts[job] = "proven"
        else:
            verdicts[job] = "none"
    return verdicts


@dataclass(frozen=True)
class Agreement:
    """The comparison of a window of sweeps with hosted CI.

    Attributes:
        sweeps: The sweeps compared.
        agree: Checks whose two verdicts match.
        red_here_only: Each sweep with a check red in the sweep and green hosted, and those
            checks' jobs.
        red_hosted_only: Each sweep with a check green in the sweep and red hosted, and those
            checks' jobs.
        not_comparable: Checks where either side has no verdict.
        hosted_ran: Compared checks that hosted CI ran itself, not proven.
        hosted_red: Compared checks that hosted CI read red.
    """

    sweeps: tuple[Swept, ...]
    agree: int
    red_here_only: tuple[tuple[Swept, tuple[str, ...]], ...]
    red_hosted_only: tuple[tuple[Swept, tuple[str, ...]], ...]
    not_comparable: int
    hosted_ran: int
    hosted_red: int

    @property
    def compared(self) -> int:
        """Checks with a verdict on both sides."""
        disagree = sum(
            len(jobs) for _, jobs in self.red_here_only + self.red_hosted_only
        )
        return self.agree + disagree

    def days(self, now: float) -> float:
        """Days from the window's first sweep to `now`."""
        return (now - self.sweeps[0].at) / 86400

    def p90_seconds(self) -> int:
        """The nearest-rank 90th percentile of a whole sweep's duration."""
        ranked = sorted(one.sweep.seconds for one in self.sweeps)
        return ranked[(9 * len(ranked) + 9) // 10 - 1]

    def gate_met(self, now: float) -> bool:
        """Whether slice B2 may start.

        It needs the days, the compared checks of `GATE_SWEEPS` sweeps, the speed, enough
        hosted reds for a miss to have been possible, and no miss.
        """
        return (
            self.days(now) >= GATE_DAYS
            and self.compared >= GATE_SWEEPS * len(HOSTED_JOBS)
            and self.p90_seconds() <= GATE_P90_SECONDS
            and self.hosted_red >= GATE_HOSTED_REDS
            and not self.red_hosted_only
        )


def compare(sweeps: list[Swept], hosted_at: dict[str, dict[str, str]]) -> Agreement:
    """Set each check of each sweep beside the hosted verdict of the same tree and job.

    Args:
        sweeps: The sweeps to compare.
        hosted_at: `hosted()` of what GitHub returned for each sweep's `hosted_at`.
    """
    agree = not_comparable = ran = red = 0
    here, there = [], []
    for one in sweeps:
        red_here, red_hosted = [], []
        for check in one.sweep.checks:
            other = hosted_at.get(one.hosted_at, {}).get(check.job, "none")
            if "none" in (check.verdict, other):
                not_comparable += 1
                continue
            ran += other != "proven"
            red += other == "fail"
            if (check.verdict == "fail") == (other == "fail"):
                agree += 1
            elif check.verdict == "fail":
                red_here.append(check.job)
            else:
                red_hosted.append(check.job)
        if red_here:
            here.append((one, tuple(red_here)))
        if red_hosted:
            there.append((one, tuple(red_hosted)))
    return Agreement(
        tuple(sweeps), agree, tuple(here), tuple(there), not_comparable, ran, red
    )


def _listed(
    found: tuple[tuple[Swept, tuple[str, ...]], ...], with_output: bool
) -> list[str]:
    lines = []
    for one, jobs in found:
        when = time.strftime("%Y-%m-%d %H:%MZ", time.gmtime(one.at))
        run = f" of run {one.sweep.run}" if one.sweep.run else ""
        lines.append(f"  {one.sweep.sha[:12]}{run}  {', '.join(jobs)}  swept {when}")
        if with_output:
            # The run's first line is its count, which names nothing.
            lines += [f"    {line.strip()}" for line in one.printed[1:]]
    return lines


def report(found: Agreement, now: float) -> tuple[str, int]:
    """The printed report and the exit code."""
    if not found.sweeps:
        return (
            "0 sweeps in the window; the unit is homelab-local-sweep.service on daniel-box",
            PROBE_NOT_APPLICABLE,
        )
    days, p90 = found.days(now), found.p90_seconds()
    here = sum(len(jobs) for _, jobs in found.red_here_only)
    there = sum(len(jobs) for _, jobs in found.red_hosted_only)
    count = len(found.sweeps)
    replays = sum(1 for one in found.sweeps if one.sweep.run)
    lines = [
        f"{count} sweep{'s' if count != 1 else ''}, {replays} of them of pull-request runs, "
        f"over {days:.1f} days, p90 {p90}s: "
        f"{found.agree} checks agree, {here} red here only, {there} red hosted only, "
        f"{found.not_comparable} not comparable; hosted CI ran {found.hosted_ran} of the "
        f"{found.compared} compared itself, and {found.hosted_red} were red"
    ]
    if found.red_hosted_only:
        lines.append("green here and red hosted, a red tree a queue would have merged:")
        lines += _listed(found.red_hosted_only, with_output=False)
    if found.red_here_only:
        lines.append(
            "red here and green hosted, a good pull request a queue would refuse:"
        )
        lines += _listed(found.red_here_only, with_output=True)
    met = "met" if found.gate_met(now) else "not met"
    lines.append(
        f"B2's gate is {met}: {days:.1f} of {GATE_DAYS} days, {found.compared} of "
        f"{GATE_SWEEPS * len(HOSTED_JOBS)} checks compared, p90 {p90}s of "
        f"{GATE_P90_SECONDS}s allowed, {found.hosted_red} of {GATE_HOSTED_REDS} hosted reds "
        f"seen, {there} red hosted only of 0 allowed"
    )
    return "\n".join(lines), PROBE_FAILED if found.red_hosted_only else PROBE_OK


def _loki_rows(seconds: int, now: float) -> Rows:
    base, pin = core.loki_endpoint()
    start, end = trailing_window_ns(seconds, now)
    url = loki_query_url(base, LOGQL, LIMIT, start=start, end=end, direction="forward")
    return core._rows_from_loki(core.fetch_parsed(url, resolve=pin))


def _check_runs(hosted_at: str) -> CheckRuns:
    """A commit's check-runs or a run's jobs, each as its id, name, status and conclusion.

    Args:
        hosted_at: A `Swept.hosted_at`.

    Raises:
        subprocess.SubprocessError: gh failed or did not answer.
        OSError: gh is not installed.
        ValueError: gh printed a line that is not JSON.
    """
    path = f"repos/{{owner}}/{{repo}}/{hosted_at}?per_page=100"
    each = "(.check_runs // .jobs)[] | {id, name, status, conclusion}"
    out = gh("api", "--paginate", path, "--jq", each, cwd=REPO, timeout=60).stdout
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def _read_all(read: Callable[[str], CheckRuns], paths: list[str]) -> list[CheckRuns]:
    """`read` of each path, eight at a time. The first failure cancels the reads not started."""
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(read, path) for path in paths]
        try:
            return [future.result() for future in futures]
        except BaseException:
            pool.shutdown(cancel_futures=True)
            raise


def run_sweep_agreement(
    ns,
    read_rows: Callable[[int, float], Rows] = _loki_rows,
    read_check_runs: Callable[[str], CheckRuns] = _check_runs,
    now: float | None = None,
) -> int:
    """Read the window's sweeps and their hosted runs, print the report, return the exit code.

    Raises:
        core.Unreadable: the window fills Loki's limit, or GitHub could not be read.
    """
    now = time.time() if now is None else now
    rows = read_rows(core.parse_duration_seconds(ns.since), now)
    if len(rows) >= LIMIT:
        raise core.Unreadable(
            f"the window holds {LIMIT} lines or more, which is all one Loki query returns, so "
            "sweeps are missing from it; pass a shorter --since"
        )
    sweeps = swept(rows)
    paths = [one.hosted_at for one in sweeps]
    try:
        hosted_at = dict(
            zip(paths, map(hosted, _read_all(read_check_runs, paths)), strict=True)
        )
    except (subprocess.SubprocessError, OSError, ValueError, KeyError) as exc:
        cause = (getattr(exc, "stderr", None) or str(exc)).strip().splitlines()[-1:]
        raise core.Unreadable(
            f"GitHub's check-runs could not be read ({''.join(cause)}); check "
            "`gh auth status`, then run this again"
        ) from exc
    text, code = report(compare(sweeps, hosted_at), now)
    print(text)
    return code


def add_parser(sub, verdict_help) -> None:
    """Add `sweep-agreement` to probe.py's subparsers."""
    parser = sub.add_parser("sweep-agreement", **verdict_help(*SWEEP_AGREEMENT_HELP))
    parser.add_argument(
        "--since", default="8d", help="the window of sweeps, e.g. 2d or 1w"
    )
