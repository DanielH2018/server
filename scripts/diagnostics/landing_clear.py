"""The `landing-clear` source for cc-wait: whether anything in CLAUDE.md's *When to wait* blocks.

`cc-wait landing-clear` runs this through `.claude/wait-sources/landing-clear`. A session that
stopped on a red master CI or a set `DEPLOY_HOLD` waits here and resumes its landing once
neither holds. Clearing the hold stays the operator's decision; this only waits for it.

    landing_clear.py --describe   # terminal states, as JSON
    landing_clear.py              # the state now, one JSON line

ONE DEFINITION OF A BLOCKER. The verdict is `landing_blockers.blockers` over the three facts it
reads, and the CI state is `landing_blockers.ci_state`, so this wait and `probe.py landing` cannot
disagree on what blocks. The other reads `collect` makes (the cluster,
GHCR, deploy-ui, worktrees, claims) block nothing and are not made.

The third fact is a live landing of a serial PR (`live_limit.SERIAL_WHAT`), which holds the
serial lock and makes `land.sh` refuse every other landing. Both tools read it through
`serial_lock.serial_landing`.

THE STATE.

    blocked             (open)  a blocker holds; the detail names each one
    clear               0       DEPLOY_HOLD reads `none` or is absent, master CI is not red,
                                and no serial landing is live
    gh-unauthenticated  1       `gh` has no login configured, so no read can succeed

A pending or cancelled master run is not red (`docs/landing.md`), so it reads `clear`:
`land.sh` waits on a pending run itself.

A FAILED READ IS NEVER CLEAR. `collect` leaves a failed source `null` and `blockers` reads
`null` as no blocker, which is right for a band and wrong for a gate. So this reads both facts
itself and exits 1 on any failure, and cc-wait retries the read.

ONE FAILURE NO WAIT FIXES. cc-wait refuses a source that declares no terminal state with a
non-zero code, since a source that can only succeed reads a crash as still waiting. `gh` exits
4 when no login is configured (`gh help exit-codes`), and no retry changes that, so it ends the
wait as `gh-unauthenticated`. A rejected token is HTTP 401, which `gh` exits 1 for, so it stays
a failed read. The CI read goes first because it sees `gh`'s exit code; the hold read reports
only text.

THE COST. Each read is two REST calls through `lib.gh`, so the quota ledger counts them
(docs/github-quota.md): `gh variable get DEPLOY_HOLD` and one page of master `ci.yml` runs
through `gh api`. At a 180s interval that is 40 calls an hour.
"""

import json
import socket
import sys
from collections.abc import Callable
from subprocess import CompletedProcess

from deploy_tools import forge
from deploy_tools.land_lib.serial_lock import serial_landing
from diagnostics.probe_lib.landing_blockers import blockers, ci_state, serial_state
from diagnostics.probe_lib.promote_state import LIVE_PROMOTE_READS
from lib.cli_help import ArgumentParser, refuse_help_under_describe
from lib.exit_codes import FAILED, OK
from lib.gh import gh

from lib import host_modules  # noqa: F401
from ci_verdict import WorkflowRun

TERMINAL = {"clear": OK, "gh-unauthenticated": FAILED}
GH_AUTH_REQUIRED = 4
INTERVAL_S = 180
READ_TIMEOUT_S = 20
DETAIL_MAX = 600


class NoGhLogin(Exception):
    """`gh` has no login configured, so every GitHub read fails until someone logs in."""


def _master_ci_run(
    run_gh: Callable[..., CompletedProcess[str]] = gh,
) -> WorkflowRun | None:
    """The newest `ci.yml` run on master, or None when there is none.

    Args:
        run_gh: runs `gh` with these arguments; `lib.gh.gh` unless a test hands in a fake.

    Raises:
        NoGhLogin: `gh` exited 4, its code for a command that needs a login.
        RuntimeError: any other failed call, which cc-wait retries.
    """
    proc = run_gh(
        "api",
        f"repos/{forge.REPOSITORY}/actions/workflows/ci.yml/runs?branch=master&per_page=1",
        check=False,
        timeout=READ_TIMEOUT_S,
    )
    if proc.returncode == GH_AUTH_REQUIRED:
        raise NoGhLogin(proc.stderr.strip())
    if proc.returncode != 0:
        raise RuntimeError(f"gh api exited {proc.returncode}: {proc.stderr.strip()}")
    runs = json.loads(proc.stdout)["workflow_runs"]
    return WorkflowRun.parse(runs[0]) if runs else None


def read(
    deploy_hold: Callable[[], str | None] = LIVE_PROMOTE_READS.deploy_hold,
    master_ci_run: Callable[[], WorkflowRun | None] = _master_ci_run,
    serial: Callable[[], str | None] = lambda: serial_landing(socket.gethostname()),
) -> dict:
    """The landing's state now, as cc-wait's state and detail.

    Args:
        deploy_hold: reads DEPLOY_HOLD; None means the variable is absent. Raises on failure.
        master_ci_run: reads the newest master `ci.yml` run. Raises on failure.
        serial: reads the PR whose landing holds the serial lock, None while none does;
            `land.sh` refuses every other landing meanwhile. Raises on failure.
    """
    try:
        run = master_ci_run()
    except NoGhLogin as exc:
        return {
            "state": "gh-unauthenticated",
            "detail": " ".join(str(exc).split())[:DETAIL_MAX],
        }
    hold = deploy_hold()
    ci = {
        "state": ci_state(run),
        "sha": run.head_sha[:8] if run else "",
        "url": run.url if run else "",
    }
    found = blockers(
        {
            "promote": {"deploy_hold": hold},
            "ci": ci,
            "serial": serial_state(serial()),
        }
    )
    if found:
        return {"state": "blocked", "detail": " | ".join(found)[:DETAIL_MAX]}
    detail = f"DEPLOY_HOLD {hold or 'none'}; master CI {ci['state']} {ci['sha']}"
    return {"state": "clear", "detail": detail.strip()}


def describe() -> dict:
    return {"terminal": TERMINAL, "interval_s": INTERVAL_S}


def main(argv: list[str] | None = None, read_state: Callable[[], dict] = read) -> int:
    parser = ArgumentParser(
        prog="landing_clear.py", description=__doc__.splitlines()[0]
    )
    parser.add_argument("--describe", action="store_true")
    refuse_help_under_describe(parser, argv)
    ns = parser.parse_args(argv)
    if ns.describe:
        print(json.dumps(describe()))
        return 0
    try:
        print(json.dumps(read_state()))
    except Exception as exc:
        print(f"landing_clear: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
