#!/usr/bin/env python3
"""Run crowdsec's maintenance jobs on their schedules, inside the crowdsec pod.

The `crowdsec-jobs` sidecar of the crowdsec Deployment runs this module as its only process.
Each job needs `cscli` against the LAPI's own database, and only a container in the LAPI's pod
can run that without a `pods/exec` grant. So the jobs run here, beside the engine, rather than
as CronJobs or as daniel-box root crons (the operator's rulings of 2026-10-18 on #5655 and
#5701, recorded in the H2 plan's *Open questions* 2).

Four jobs run, each on its old host cron's UTC schedule:

    crowdsec-remote-allowlist  remote_allowlist.py  every 5 minutes  pushes CrowdSec Remote Allowlist
    crowdsec-appsec-verify     appsec_verify.py     every 15 minutes pushes CrowdSec AppSec
    crowdsec-bouncer-prune     bouncer_prune.py     at minute 17     logs only
    crowdsec-home-allowlist    home_allowlist.py    every 5 minutes, pushes CrowdSec Home Allowlist
                                                    one minute after the crowdsec-home-ips CronJob

Each run is a child `python3` process, so a job that crashes or hangs cannot stop this one. A
run that outlives its timeout is killed and reported as down. A job still running when its next
slot comes is skipped for that slot, as `concurrencyPolicy: Forbid` would skip it. This process
does what each job's bash wrapper did on the host: it takes the child's exit code and last
output line as the verdict, prints `<tag>: status=<up|down> <summary>` to stdout, and pushes the
verdict to the job's Kuma monitor. Each stdout line carries the host crons' syslog prefix,
`<UTC time> <pod name> <tag>: <message>`, and is flushed as it is written. monitor-bridge's
Swallowed Push Verdicts check and `probe.py alerts` both parse that shape from
`{container="crowdsec-jobs"}`. The push URL comes from the environment variable the job
names, which the pod reads from the `crowdsec-jobs` Secret.

No job runs in the first `STARTUP_GRACE_S` seconds. The engine container writes
`/etc/crowdsec` on start, and a job that ran before it finished would page a fault that does
not exist.

Usage:

    python3 /jobs/crowdsec_jobs.py               # the sidecar's command; runs until SIGTERM
    python3 /jobs/crowdsec_jobs.py --run <tag>   # one run of one job now, then exit

`--run` exits with the job's own exit code and pushes as a scheduled run does. `-h` and
`--help` print this text and exit 0.
"""

import os
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import kuma_push

JOBS_DIR = Path(__file__).resolve().parent
STARTUP_GRACE_S = 120


class Running(Protocol):
    """A started run, as `tick` reads it: only whether it is still going."""

    def is_alive(self) -> bool: ...


@dataclass(frozen=True)
class Job:
    """One scheduled job.

    Attributes:
        tag: the name its log lines start with, which was its syslog tag on the host.
        script: the module under JOBS_DIR that the run starts.
        period: minutes between runs.
        offset: the minute of each period the run starts at.
        timeout_s: how long a run may take before it is killed and reported down. Each is
            below its period, so a hung run is reported before its next slot.
        push_env: the environment variable holding the job's Kuma push URL, or None for a
            job with no Kuma monitor.
    """

    tag: str
    script: str
    period: int
    offset: int
    timeout_s: int
    push_env: str | None


JOBS = (
    Job(
        "crowdsec-remote-allowlist",
        "remote_allowlist.py",
        period=5,
        offset=0,
        timeout_s=180,
        push_env="REMOTE_ALLOWLIST_PUSH_URL",
    ),
    Job(
        "crowdsec-appsec-verify",
        "appsec_verify.py",
        period=15,
        offset=0,
        timeout_s=300,
        push_env="APPSEC_PUSH_URL",
    ),
    # No Kuma monitor: a failed run only lets bouncer rows accumulate until the next run
    # succeeds. Its log line is the evidence (the role's CLAUDE.md).
    Job(
        "crowdsec-bouncer-prune",
        "bouncer_prune.py",
        period=60,
        offset=17,
        timeout_s=600,
        push_env=None,
    ),
    # One minute after each crowdsec-home-ips CronJob slot, so each run reads the lookup that
    # slot wrote rather than the one before it.
    Job(
        "crowdsec-home-allowlist",
        "home_allowlist.py",
        period=5,
        offset=1,
        timeout_s=180,
        push_env="HOME_ALLOWLIST_PUSH_URL",
    ),
)


def emit(
    line: str,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    host: Callable[[], str] = socket.gethostname,
    write: Callable[..., None] = print,
) -> None:
    """Prints one `<tag>: <message>` line to stdout in the host crons' syslog shape.

    The prefix is the two whitespace-free tokens rsyslog writes, an ISO time and the host, here
    the pod name: the readers of this stream match exactly two tokens before the tag. The flush
    is per line, because a pipe is block-buffered, and an OOM kill would discard a buffered
    verdict before the container log ever held it.
    """
    stamp = now().strftime("%Y-%m-%dT%H:%M:%SZ")
    write(f"{stamp} {host()} {line}", flush=True)


def due(job: Job, minute: int) -> bool:
    """Whether `job` starts at `minute` of the UTC hour."""
    return minute % job.period == job.offset


def outcome(job: Job, returncode: int, output: str) -> tuple[str, str]:
    """`(status, summary)` from one run: `up` on exit 0, and the last line of its output."""
    lines = [line for line in output.splitlines() if line.strip()]
    summary = lines[-1] if lines else f"{job.script} exited {returncode} with no output"
    return ("up" if returncode == 0 else "down"), summary


def run_job(
    job: Job,
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    push: Callable[..., bool] = kuma_push.kuma_push,
    env: Mapping[str, str] | None = None,
    out: Callable[[str], None] = emit,
) -> int:
    """Runs `job` once, prints its verdict and pushes it.

    Returns:
        The run's exit code, 1 for a run killed at its timeout.
    """
    environ: Mapping[str, str] = os.environ if env is None else env
    try:
        done = runner(
            [sys.executable, str(JOBS_DIR / job.script)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=job.timeout_s,
        )
        returncode, output = done.returncode, done.stdout or ""
    except subprocess.TimeoutExpired:
        returncode, output = (
            1,
            f"{job.script} killed after its {job.timeout_s}s timeout",
        )
    status, summary = outcome(job, returncode, output)
    if returncode != 0:
        # The summary is the last line. The lines above it, a traceback or a cscli error, are
        # the evidence for the failure, and a pod log is the only place they survive.
        for line in output.splitlines()[:-1]:
            out(f"{job.tag}: {line}")
    out(f"{job.tag}: status={status} {summary}")
    if job.push_env:
        base, _, token = environ.get(job.push_env, "").partition("/api/push/")
        push(status, summary, base, token, log=lambda line: out(f"{job.tag}: {line}"))
    return returncode


def start_thread(job: Job) -> threading.Thread:
    thread = threading.Thread(target=run_job, args=(job,), name=job.tag, daemon=True)
    thread.start()
    return thread


def tick(
    minute: int,
    running: dict[str, Running],
    *,
    start: Callable[[Job], Running] = start_thread,
    out: Callable[[str], None] = emit,
) -> None:
    """Starts every job due at `minute` whose previous run has finished."""
    for job in JOBS:
        if not due(job, minute):
            continue
        previous = running.get(job.tag)
        if previous is not None and previous.is_alive():
            out(
                f"{job.tag}: skipped minute {minute:02d}, the previous run is still going"
            )
            continue
        running[job.tag] = start(job)


def main(
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    start: Callable[[Job], Running] = start_thread,
    minutes: int | None = None,
    grace_s: float = STARTUP_GRACE_S,
) -> int:
    """Runs the schedule until SIGTERM, or for `minutes` minute boundaries when a test sets it.

    A wake before the boundary sleeps again. The minute passed to `tick` is the boundary it slept
    to, not the clock read after waking, so a wake that runs late, past the next boundary, still
    starts the jobs of the minute it waited for. The minutes it overslept are not made up.
    """
    started = clock()
    running: dict[str, Running] = {}
    while minutes is None or minutes > 0:
        boundary = (int(clock() // 60) + 1) * 60
        while (now := clock()) < boundary:
            sleep(boundary - now)
        if boundary - started >= grace_s:
            tick(boundary // 60 % 60, running, start=start)
        if minutes is not None:
            minutes -= 1
    return 0


if __name__ == "__main__":
    if any(arg in ("-h", "--help") for arg in sys.argv[1:]):
        print(__doc__.strip())
        sys.exit(0)
    if sys.argv[1:2] == ["--run"]:
        by_tag = {job.tag: job for job in JOBS}
        if sys.argv[2:3] == [] or sys.argv[2] not in by_tag:
            print(f"--run takes one of: {', '.join(by_tag)}", file=sys.stderr)
            sys.exit(2)
        sys.exit(run_job(by_tag[sys.argv[2]]))
    # PID 1 in a container ignores a signal it installs no handler for, so without this the
    # kubelet waits out the grace period on every engine restart before it kills the pod.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    sys.exit(main())
