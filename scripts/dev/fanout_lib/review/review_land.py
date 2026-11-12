"""The review pipeline's landing: `land.sh` run by the pipeline, the model resumed only on need.

WHY (#3960). The land phase used to resume the implementer with the brief's Landing section
and let it run `land.sh --detach && cc-wait land <n>` itself. The model got the mechanics
wrong: eight exit-75 re-runs, calls to the retired `--await-verdict`, a bad cc-wait source. A
session holding its turn open through CI and the promotion also kept the batch's 2 GiB
reservation, which bounds fan-out width. The pipeline now starts the landing and waits on it,
and resumes the implementer only for a verdict that needs judgement: `RESUME_VERDICTS`.

THE COMMANDS run under `bash -l`, because the model ran them from its Bash tool, which loads
the login profile, and the unit does not. The `claude` agent user's profile sets
`LAND_HANDOFF_UNIT`, without which its `land.sh` tries to merge as a user that cannot.

DECIDED: `land.sh` runs from a fresh detached worktree at the target's base (#7871), not the
batch worktree. The batch worktree is branched at launch, and `land.sh` refuses to run once
master has changed its own landing code since (#7831), so any such change between launch and
landing ended the batch `needs input`. The staleness check stays as it is. The implementer
already runs arbitrary commands as this user, so the base's copy adds no boundary, and for
the agent user the landing itself runs in `homelab-claude-land@<n>.service` from the
operator's checkout either way.
"""

import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

LAND_SH = Path("scripts") / "deploy_tools" / "land.sh"
# The verdicts after which an apply is owed or the deploy needs a look. The landing is
# finished for every other verdict, or gave up at a resume point an operator re-runs.
RESUME_VERDICTS = frozenset({"unhealthy", "deploy-failed", "needs-manual-apply"})
# The verdicts after which the PR is merged and nothing more is owed. `deferred` is one: a host's
# updater applies its package, and cc-wait ends it with exit 4, not 0.
FINISHED_VERDICTS = frozenset({"settled", "nothing-to-deploy", "deferred"})
# cc-wait's "still running, re-run this wait". land.sh's own give-up reaches it as 3.
STILL_WAITING = 75
# A wait started with less than this left on the unit's run time could be cut off mid-wait.
WAIT_MARGIN_S = 600
VERDICT_LINE = re.compile(r"(?m)^VERDICT: (\S+).*$")
TAIL_LINES = 40

Runner = Callable[[list[str], str | None], subprocess.CompletedProcess]


@dataclass
class Landing:
    """One landing's result, as the log it wrote records it.

    Attributes:
        rc: the exit code of the last command run: land.sh's when it refused to start,
            else cc-wait's, which is the landing's own code once the landing has ended.
        verdict: the VERDICT token, "" when the log holds none.
        line: the whole VERDICT line, "" when the log holds none.
        tail: the last lines of the landing's log, or of land.sh's output when it refused.
        wait: the command that resumes the wait, for a landing still running at the deadline.
        checkout: the fresh checkout `land.sh` ran from, set only for a verdict in
            `RESUME_VERDICTS`, whose resumed implementer re-lands from it. The caller
            removes it with `release` once that session ends.
    """

    rc: int
    verdict: str = ""
    line: str = ""
    tail: str = ""
    wait: str = ""
    checkout: Path | None = None


def login(argv: list[str]) -> list[str]:
    return ["bash", "-lc", 'exec "$@"', "land", *argv]


def _tail(text: str) -> str:
    return "\n".join(text.strip().splitlines()[-TAIL_LINES:])


def _newest_log(log_dir: Path, pr: str) -> Path | None:
    # `detach.log_path` stamps each log, so the newest name is this landing's.
    logs = sorted(log_dir.glob(f"land{pr}-*.log"))
    return logs[-1] if logs else None


def _git(run: Runner, worktree: Path, *args: str) -> subprocess.CompletedProcess:
    return run(["git", "-C", str(worktree), *args], None)


def _release(run: Runner, worktree: Path, path: Path) -> None:
    _git(run, worktree, "worktree", "remove", "--force", str(path))
    _git(run, worktree, "worktree", "prune")


def _fresh_checkout(
    run: Runner, worktree: Path, base: str, pr: str
) -> tuple[Path, str]:
    """A detached worktree at `base` for this PR's landing, and why it could not be made.

    It lives in the batch worktree's `.fanout` directory, which git ignores. It shares the
    batch worktree's refs, so the fetch here is the one `land.sh` compares against.
    """
    path = worktree / ".fanout" / f"land-checkout-{pr}"
    _release(run, worktree, path)
    for args in (
        ("fetch", "--quiet", "origin", base.split("/", 1)[1]),
        ("worktree", "add", "--detach", str(path), base),
    ):
        done = _git(run, worktree, *args)
        if done.returncode != 0:
            return path, _tail(done.stdout + done.stderr)
    return path, ""


def land(
    run: Runner,
    worktree: Path,
    pr_url: str,
    time_left: Callable[[], float],
    base: str,
) -> Landing:
    """Start the PR's landing from a fresh checkout of `base`, wait, and read its verdict.

    `land.sh` runs exactly once. A wait that exits 75 is re-run on its own while
    `time_left()` allows, and a landing still running past that is returned with `wait` set.
    The checkout is removed once the landing has ended. It is kept while the landing still
    runs, and after a verdict in `RESUME_VERDICTS`, for the resumed implementer's re-land.
    """
    pr = pr_url.rstrip("/").rsplit("/", 1)[-1]
    log_dir = worktree / ".fanout"
    checkout, why = _fresh_checkout(run, worktree, base, pr)
    if why:
        _release(run, worktree, checkout)
        return Landing(1, tail=f"could not make a checkout at {base}:\n{why}")
    started = run(
        login(
            [
                str(checkout / LAND_SH), "--pr", pr, "--arm-merge", "--await-merge",
                "--detach", "--log-dir", str(log_dir),
            ]
        ),
        None,
    )  # fmt: skip
    if started.returncode != 0:
        _release(run, worktree, checkout)
        return Landing(started.returncode, tail=_tail(started.stdout + started.stderr))
    wait = ["cc-wait", "land", pr, "--log-dir", str(log_dir)]
    waited = run(login(wait), None)
    while waited.returncode == STILL_WAITING and time_left() > WAIT_MARGIN_S:
        waited = run(login(wait), None)
    log = _newest_log(log_dir, pr)
    text = log.read_text(errors="replace") if log else ""
    matches = list(VERDICT_LINE.finditer(text))
    found = matches[-1] if matches else None
    verdict = found.group(1) if found else ""
    resume = verdict in RESUME_VERDICTS
    if waited.returncode != STILL_WAITING and not resume:
        _release(run, worktree, checkout)
    return Landing(
        waited.returncode,
        verdict=verdict,
        line=found.group(0) if found else "",
        tail=_tail(text or waited.stdout + waited.stderr),
        wait=" ".join(wait) if waited.returncode == STILL_WAITING else "",
        checkout=checkout if resume else None,
    )


def release(run: Runner, worktree: Path, landing: Landing) -> None:
    """Remove the checkout a landing kept for its resumed implementer's re-land."""
    if landing.checkout:
        _release(run, worktree, landing.checkout)


def out_of_time(pr: str) -> dict:
    """The batch's final report when the review phases left too little run time to land."""
    text = (
        "needs input: the review phases left too little of the unit's run "
        f"time to land safely; land {pr} by hand."
    )
    return {"type": "result", "is_error": False, "result": f"{text}\n{pr}"}


def report(pr: str, landing: Landing) -> dict:
    """The batch's final report for a landing the implementer is not resumed for.

    A finished landing reads `done` in `status`: the VERDICT line and the PR URL. Anything
    else is a `needs input:` line, because no session is left to act on it.
    """
    if landing.wait:
        text = f"needs input: the landing of {pr} is still running; wait with `{landing.wait}`"
    elif not landing.line:
        text = (
            f"needs input: land.sh printed no VERDICT for {pr} (exit {landing.rc}):\n"
            f"{landing.tail}"
        )
    elif landing.verdict not in FINISHED_VERDICTS:
        text = f"needs input: the landing of {pr} stopped short.\n{landing.line}"
    else:
        text = landing.line
    return {"type": "result", "is_error": False, "result": f"{text}\n{pr}"}
