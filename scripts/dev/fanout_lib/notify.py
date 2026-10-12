"""Page the operator when a fan-out batch's unit stops on any state but `done` (#4602).

`launch.systemd_run_command` runs this as the unit's `ExecStopPost=`, with the batch's
worktree as its cwd. It classifies the batch exactly as `fanout.py place status` would, from
the same files and from the stop result systemd hands an `ExecStopPost=` in its environment,
then starts `fanout-alert@<unit>:<state>.service`. That system unit holds the Discord webhook,
which this user never reads; polkit (`50-claude-fanout-alert.rules`) lets the user start it
for a fixed set of states and nothing else.

Before this, `status` and `cc-wait fanout` read a blocked batch only while an orchestrator
session was alive to ask, and the orchestrator dies with its session.

Usage::

    notify.py --batch 4602 --unit fanout-4602 --branch worktree-fanout-4602 \
        --repo DanielH2018/server
"""

import os
import shlex
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path

# Reach the sibling package and `lib` (scripts/): a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys

_sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fanout_lib.brief import _snapshot_root
from fanout_lib.manifest import Batch
from fanout_lib.target import HEADLESS_PYTHON, Target, branch_name
from fanout_lib.status import (
    NEEDS_INPUT,
    NO_PR,
    NO_REPORT,
    NO_VERDICT,
    VERDICT,
    BatchStatus,
    parse_status,
    reconciled,
)
from fanout_lib.transport import merged_pr_url, this_host
from lib.cli_help import ArgumentParser
from lib.exit_codes import OK

# Every state `parse_status` or `reconciled` gives a stopped unit that pages. `done` and
# `landed` do not. The polkit rule admits exactly these, and a test holds the two equal.
PAGED_STATES = ("failed", NEEDS_INPUT, NO_PR, NO_VERDICT, NO_REPORT)
ALERT_UNIT = "fanout-alert@{unit}:{state}.service"
START_TIMEOUT_S = 30.0
# This file, relative to a checkout, as `launch.REVIEW_SCRIPT` is.
NOTIFY_SCRIPT = "scripts/dev/fanout_lib/notify.py"


# DECIDED: the notifier runs from the batch's own worktree, and does not run when that tree
# is gone. A batch whose tree vanished before its unit stopped was removed by the agent's own
# tidy-up or by `clean` after its PR landed, which `status` reads as `no-report` and almost
# always reconciles to `landed`. The `-` prefix makes the failed chdir harmless, and running
# the host's primary checkout instead would run a copy that can lag `origin/master` (#3684).
# Its output goes to `.fanout/notify.log`, because systemd opens the unit's `file:` outputs
# without truncating them: a line on stdout would overwrite the head of `report.json`.
# Both behaviours were measured with transient user units on systemd 255.
def stop_post(batch: str, target: Target) -> str:
    """The quoted `ExecStopPost=` property `launch.systemd_run_command` gives a batch's unit.

    Another repo's worktree does not carry this file, so its batch runs the copy in its
    snapshot of this repo, as `launch.review_script` does.
    """
    script = NOTIFY_SCRIPT
    if not target.is_server:
        script = f"{_snapshot_root(batch, target)}/{NOTIFY_SCRIPT}"
    command = (
        f"exec uv run --no-project --no-python-downloads --python {HEADLESS_PYTHON} "
        f"{script} --batch {batch} --unit {target.unit_prefix}-{batch} "
        f"--branch {branch_name(batch)} --repo {target.repo} >>.fanout/notify.log 2>&1"
    )
    return shlex.quote(f'ExecStopPost=-/bin/sh -c "{command}"')


def _tail(path: Path, size: int = 2000) -> str:
    try:
        return path.read_bytes()[-size:].decode(errors="replace")
    except OSError:
        return ""


def _last_verdict(fanout: Path) -> str:
    lines = [
        line
        for log in sorted(fanout.glob("land*.log"))
        for line in _tail(log, 1 << 20).splitlines()
        if VERDICT.match(line)
    ]
    return lines[-1] if lines else ""


def status_block(batch: str, env: Mapping[str, str], fanout: Path) -> str:
    """The text `status_command` would print for this batch, read on the stopping unit.

    A `systemctl show` of the unit during its own `ExecStopPost=` reads `deactivating` with a
    `Result` not yet final, so the result comes from what systemd sets for this command:
    `SERVICE_RESULT`, and `EXIT_STATUS`, a number, or a signal name when the process was
    killed.
    """
    return (
        f"=== {batch}\n"
        "ActiveState=deactivating\n"
        f"Result={env.get('SERVICE_RESULT', '')}\n"
        f"ExecMainStatus={env.get('EXIT_STATUS', '')}\n"
        f"--- stderr\n{_tail(fanout / 'stderr.log')}\n"
        f"--- report\n{_tail(fanout / 'report.json', 1 << 22)}\n"
        f"--- verdict\n{_last_verdict(fanout)}\n"
    )


def stopped_on_request(env: Mapping[str, str]) -> bool:
    """Whether systemd stopped the unit cleanly, as `fanout.py place stop` does.

    The process is killed by signal and systemd still records success. Whoever ran the stop
    already knows about it, so it does not page. A `RuntimeMaxSec=` expiry records `timeout`
    and an OOM kill records `oom-kill`, so both still page.
    """
    return env.get("SERVICE_RESULT") == "success" and env.get("EXIT_CODE") == "killed"


def classify(
    b: Batch,
    env: Mapping[str, str],
    fanout: Path,
    merged_pr: Callable[[str, str], str] = merged_pr_url,
) -> BatchStatus:
    """`b`'s state as `status` reads it, a `no-report` reconciled against a merged PR."""
    (s,) = parse_status([b], status_block(b.batch, env, fanout))
    return reconciled(s, b.branch, merged_pr, b.repo)


def alert_unit(unit: str, state: str) -> str:
    return ALERT_UNIT.format(unit=unit, state=state)


def page_command(unit: str, state: str) -> list[str]:
    """Start the alert without waiting on it: its delivery retries for as long as it takes."""
    return [
        "systemctl",
        "start",
        "--no-block",
        "--no-ask-password",
        alert_unit(unit, state),
    ]


def notify(
    b: Batch,
    env: Mapping[str, str],
    fanout: Path,
    merged_pr: Callable[[str, str], str] = merged_pr_url,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> str:
    """Page for `b` when its unit stopped on a paged state; return the line to log."""
    if stopped_on_request(env):
        return f"{b.unit}: stopped on request, no page"
    state = classify(b, env, fanout, merged_pr).state
    if state not in PAGED_STATES:
        return f"{b.unit}: {state}, no page"
    proc = run(
        page_command(b.unit, state),
        capture_output=True,
        text=True,
        timeout=START_TIMEOUT_S,
        check=False,
    )
    if proc.returncode != 0:
        return f"{b.unit}: {state}, page FAILED to start: {proc.stderr.strip()}"
    return f"{b.unit}: {state}, paged through {alert_unit(b.unit, state)}"


def main(argv: list[str] | None = None) -> int:
    p = ArgumentParser(
        prog="notify.py", description=__doc__.splitlines()[0], allow_abbrev=False
    )
    p.add_argument("--batch", required=True)
    p.add_argument("--unit", required=True)
    p.add_argument("--branch", required=True)
    p.add_argument("--repo", required=True)
    args = p.parse_args(argv)
    b = Batch(
        args.batch,
        this_host(),
        str(Path.cwd()),
        args.branch,
        args.unit,
        [],
        "",
        repo=args.repo,
    )
    print(notify(b, os.environ, Path.cwd() / ".fanout"), flush=True)
    return OK


if __name__ == "__main__":
    _sys.exit(main())
