"""Decide whether a finished batch is resumed for a merge conflict: `fanout.py place resume`.

WHY. A review batch resumes its own implementer when the landing it ran ends
`merge-conflict` (`review/conflict_resume.py`). A PR the pipeline does not land is landed by
hand after the unit has stopped, so a conflict there finds no unit. `place resume` starts one
that runs the same resume, review and landing, and this module is what it checks first.

WHAT IT READS. One call on the batch's host for the unit's state, the worktree, the batch's
own landing log and the files that conflict; the forge for the PR; one call on the landing
locks' host for a live landing of the PR.

WHERE THE VERDICT COMES FROM. `land.sh --detach` writes `land<pr>-<stamp>.log`, and the
stamp sorts by time. The newest of these is the PR's last landing:

- the batch's own, in its worktree's `.fanout/`;
- a session's own `land.sh`, in `detach.default_log_dir()`, which is where `cc-wait land`
  looks for it. That is the directory of the session that runs `place resume`, so the
  command finds a landing its caller started;
- the server lander's, under `autoland.STATE_DIR`.

`--log` names a log anywhere else, and then no other is read. The verdict is the
`LAND-RESULT:` line's `verdict` field, or the last `VERDICT:` line of a log with none.

THE BOUND. `MAX_RESUMES` per batch, counted in the manifest as `Batch.conflict_resumes`.
"""

import argparse
import json
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from deploy_tools.land_lib import detach, serial_lock
from dev.fanout_lib import launch as launch_mod
from dev.fanout_lib import launch_lock, sizing
from dev.fanout_lib import manifest as manifest_mod
from dev.fanout_lib.land_result import FINISHED_VERDICTS, last_verdict, reland_command
from dev.fanout_lib.manifest import Batch
from dev.fanout_lib.review.api import MERGE_CONFLICT
from dev.fanout_lib.target import SERVER, SERVER_TARGET
from dev.fanout_lib.transport import Tools, registered_keys
from dev.fanout_lib.unit_state import unit_props_command
from lib.cli_help import error
from lib.exit_codes import (
    FANOUT_PLACE_FAILED,
    FANOUT_PLACE_LOCK_TIMEOUT,
    FANOUT_PLACE_NO_SIGNING_HOST,
    FANOUT_PLACE_NOTHING_PLACED,
    FANOUT_PLACE_OK,
)

# How many times one batch is restarted for a conflict. The pipeline's own resume is one per
# unit, and a PR that conflicts a third time is changing files master keeps changing.
MAX_RESUMES = 2
READ_TIMEOUT_S = 60.0
# `autoland.STATE_DIR / "logs"`, restated because `place clean-one` imports this module
# with no site-packages, which `autoland`'s imports need.
LANDER_LOGS = Path.home() / ".local" / "state" / "fanout-server-lander" / "logs"
LOCK_TIMEOUT_S = 30.0
_STAMP = re.compile(r"land\d+-(.+)\.log$")
_ACTIVE = re.compile(r"(?m)^ActiveState=(\S+)")
_SECTION = re.compile(r"(?m)^--- (tree|landing|conflicts)\n")
_LIVE = frozenset({"active", "activating", "deactivating"})


@dataclass(frozen=True)
class LandingLog:
    """One landing of the PR, as the log it wrote records it.

    Attributes:
        where: the log's path, led by `<host>:` when it is on the batch's host.
        text: the log's text, or its `VERDICT:` and `LAND-RESULT:` lines alone.
    """

    where: str
    text: str

    @property
    def stamp(self) -> str:
        """The time in the log's name, which sorts the PR's landings oldest first."""
        named = _STAMP.search(self.where)
        return named.group(1) if named else ""

    @property
    def verdict(self) -> str:
        """The landing's verdict token, "" for a log that holds none."""
        fields = detach.result_fields(self.text) or {}
        return fields.get("verdict") or last_verdict(self.text)[0]

    @property
    def line(self) -> str:
        """The landing's `VERDICT:` line, or one built from the token when it printed none."""
        return last_verdict(self.text)[1] or f"VERDICT: {self.verdict}"


@dataclass(frozen=True)
class Resumable:
    """A finished batch that passed every check, and what its resumed unit is told.

    Attributes:
        batch: the batch's manifest entry.
        pr: its open PR's URL.
        landing: the landing that ended `merge-conflict`.
    """

    batch: Batch
    pr: str
    landing: LandingLog

    @property
    def request(self) -> str:
        """The text `launch.resume_batch` writes for the unit: the PR and the verdict line."""
        return json.dumps({"pr": self.pr, "line": self.landing.line})


def read_command(b: Batch) -> str:
    """One call on the batch's host: unit state, worktree, own landing log, conflicting files."""
    fanout = f"{b.worktree}/.fanout"
    git = f"git -C {b.worktree}"
    return (
        f"{unit_props_command(b)}; "
        f"echo '--- tree'; test -d {fanout} && echo standing; "
        f"echo '--- landing'; log=$(ls {fanout}/land*-*.log 2>/dev/null | tail -n 1); "
        '[ -n "$log" ] && { echo "$log"; '
        "grep -hE '^(VERDICT:|LAND-RESULT:)' \"$log\"; }; "
        f"echo '--- conflicts'; {git} fetch --quiet origin {SERVER_TARGET.base_branch}; "
        f"{git} merge-tree --write-tree --name-only --no-messages "
        f'{SERVER_TARGET.base} HEAD; echo "exit=$?"'
    )


def _sections(stdout: str) -> tuple[str, dict[str, str]]:
    parts = _SECTION.split(stdout)
    return parts[0], dict(zip(parts[1::2], parts[2::2], strict=True))


def conflicting(section: str) -> list[str]:
    """The conflicting files in `read_command`'s `conflicts` section; [] for a clean merge."""
    lines = section.strip().splitlines()
    if not lines or lines[-1] != "exit=1":
        return []
    return [line for line in lines[1:-1] if line]


def local_logs(pr: str, dirs: Sequence[Path]) -> list[LandingLog]:
    """Every landing log of PR number `pr` under `dirs` on this host."""
    found = []
    for directory in dirs:
        for path in sorted(directory.glob(f"land{pr}-*.log")):
            try:
                found.append(LandingLog(str(path), path.read_text(errors="replace")))
            except OSError:
                continue
    return found


def log_dirs() -> list[Path]:
    """Where a landing run outside the batch's unit writes its log on this host."""
    return [detach.default_log_dir(), LANDER_LOGS]


def live_landing(tools: Tools, pr: str) -> str:
    """Who holds PR number `pr`'s landing lock and how to wait on it, or "" when nobody does.

    "" too when the locks' host cannot be read: the lock refuses no landing either then.
    It reads /proc/locks and never takes the lock, so it cannot fail a landing's own attempt.
    """
    path = serial_lock.LOCK_DIR / serial_lock.lock_name(pr)
    command = f"{serial_lock.probe_command(str(path))}; cat {path} 2>/dev/null"
    try:
        proc = tools.run(serial_lock.TURN_HOST, command, LOCK_TIMEOUT_S, None)
    except subprocess.TimeoutExpired:
        return ""
    state, _, record = proc.stdout.partition("\n")
    if state.strip() != "held":
        return ""
    try:
        holder = json.loads(record)
    except ValueError:
        holder = {}
    holder = holder if isinstance(holder, dict) else {}
    log = str(holder.get("log") or "")
    who = f"host {holder.get('host') or '?'}, pid {holder.get('pid') or '?'}"
    wait = f"cc-wait land {pr}" + (f" --log {log}" if log else "")
    return f"a landing of #{pr} is live ({who}); wait with `{wait}`"


def _next_step(run_id: str, verdict: str) -> str:
    if verdict in FINISHED_VERDICTS:
        return f"it merged, so run `fanout.py place clean {run_id}`"
    return (
        f"`fanout.py place status {run_id}` names its next step; pass --log <path> when a "
        "later landing wrote its log somewhere else"
    )


def resumable(
    run_id: str,
    b: Batch,
    tools: Tools,
    log: Path | None = None,
    dirs: Sequence[Path] | None = None,
) -> Resumable | str:
    """`b` ready to resume, or the reason it is not with the command that fits instead.

    Args:
        run_id: the run `b` belongs to, for the commands a refusal names.
        b: the batch's manifest entry.
        tools: the process boundary.
        log: the landing's log, when it is in none of the places this module looks.
        dirs: where landings run outside the unit write on this host; `log_dirs()` by
            default. A test passes its own.
    """
    name = f"{b.batch} on {b.host}"
    if b.removed_at:
        issues = ",".join(str(n) for n in b.issues)
        return (
            f"{name} was cleaned ({b.removed_at}), so no worktree or session is left to "
            f"resume; relaunch its issues with `fanout.py place launch --batch {issues}`"
        )
    if b.repo != SERVER:
        return (
            f"{name} works {b.repo}, which lands through its own tooling and has no "
            "land.sh verdict to resume on; bring its branch up to date with that repo's tools"
        )
    try:
        proc = tools.run(b.host, read_command(b), READ_TIMEOUT_S, None)
    except subprocess.TimeoutExpired:
        return f"{name}: the read of its host timed out; run the same command again"
    head, sections = _sections(proc.stdout)
    active = _ACTIVE.search(head)
    if not active:
        return (
            f"{name}: its unit's state could not be read ({proc.stderr.strip()[-200:]}); "
            "run the same command again"
        )
    if active.group(1) in _LIVE:
        return (
            f"{name} is still running, and its own pipeline resumes a conflict it meets; "
            f"wait with `cc-wait fanout {run_id}`"
        )
    if "standing" not in sections.get("tree", ""):
        return (
            f"{name}: its worktree {b.worktree} is gone, and the session to resume went "
            f"with it; record that with `fanout.py place clean {run_id}`"
        )
    merged = tools.merged_pr(b.branch, b.repo)
    if merged:
        return f"{name}: {merged} merged; run `fanout.py place clean {run_id}`"
    open_branches = tools.open_pr_branches(b.repo)
    if open_branches is None:
        return (
            f"{name}: the repo's open PRs could not be read; run the same command again"
        )
    held = tools.branch_prs(b.branch, b.repo)
    urls = held.urls if held else []
    if b.branch not in open_branches or not urls:
        return (
            f"{name}: {b.branch} has no open PR, so nothing is left to land; discard the "
            f"batch with `fanout.py place abandon {run_id} {b.batch}`"
        )
    url = urls[0]
    pr = url.rstrip("/").rsplit("/", 1)[-1]
    live = live_landing(tools, pr)
    if live:
        return f"{name}: {live}, then run this again"
    dirs = log_dirs() if dirs is None else dirs
    if log is not None:
        try:
            logs = [LandingLog(str(log), log.read_text(errors="replace"))]
        except OSError as exc:
            return f"{name}: --log {log} could not be read ({exc.strerror})"
    else:
        logs = local_logs(pr, dirs)
        own = sections.get("landing", "").strip()
        if own:
            path, _, text = own.partition("\n")
            logs.append(LandingLog(f"{b.host}:{path}", text))
    if not logs:
        return (
            f"{name}: found no landing log for #{pr}, in {b.host}:{b.worktree}/.fanout "
            f"or under {', '.join(str(d) for d in dirs)}; "
            f"name it with --log <path>, or land the PR with `{reland_command(pr)}`"
        )
    last = max(logs, key=lambda entry: entry.stamp)
    if last.verdict != MERGE_CONFLICT:
        return (
            f"{name}: the newest landing of #{pr} ({last.where}) ended "
            f"`{last.verdict or 'with no verdict yet'}`, not `{MERGE_CONFLICT}`; "
            f"{_next_step(run_id, last.verdict)}"
        )
    if b.conflict_resumes >= MAX_RESUMES:
        files = conflicting(sections.get("conflicts", ""))
        return (
            f"{name} was resumed {b.conflict_resumes} times for a conflict, which is the "
            f"limit; #{pr} still conflicts with {SERVER_TARGET.base} in: "
            f"{', '.join(files) or 'git merge-tree named none'}. Resolve it by hand, or "
            f"discard the batch with `fanout.py place abandon {run_id} {b.batch}`"
        )
    return Resumable(b, url, last)


def add_resume_arguments(parser: argparse.ArgumentParser) -> None:
    """Add what `place resume` takes after its run id."""
    parser.description = (
        "Restart a finished batch's implementer to bring its PR's branch up to master "
        "after a landing ended merge-conflict, review the resolution, and land again. "
        "The pipeline lands the PR as it would have at first; a PR it does not land ends "
        "reviewed, with the land.sh command in its report."
    )
    parser.add_argument("batch")
    parser.add_argument(
        "--log",
        type=Path,
        help="the log of the landing that ended merge-conflict, when it is neither in the "
        "batch's worktree nor where this session's `land.sh --detach` writes",
    )
    # Seams for the tests: where a session's landings write, and the lock wait.
    parser.add_argument("--log-dir", type=Path, help=argparse.SUPPRESS)
    parser.add_argument(
        "--lock-wait", type=float, default=launch_lock.WAIT_S, help=argparse.SUPPRESS
    )


def cmd_resume(args, tools: Tools, readings: Callable, placement: Callable) -> int:
    """Restart one finished batch for the merge conflict its PR's newest landing ended on.

    `readings` and `placement` are `place.candidate_readings` and `place.placement`, the
    gates a launch passes, handed in because `place` imports this module.

    It holds the launch lock from its first manifest read to its manifest save, as `launch`
    does, so the live-batch cap counts this unit against a launch that runs beside it.
    """
    wait_s = args.lock_wait
    try:
        with launch_lock.held(args.manifest_root, lambda: args.run_id, wait_s):
            return _resume_locked(args, tools, readings, placement)
    except launch_lock.LockTimeout as exc:
        error(
            f"{exc} after a {wait_s:g} s wait — nothing was read or started; "
            "run the same resume again once that launch has ended"
        )
        return FANOUT_PLACE_LOCK_TIMEOUT


def _resume_locked(args, tools: Tools, readings: Callable, placement: Callable) -> int:
    """Check, gate and start the resume; the caller holds the launch lock."""
    root = args.manifest_root
    run = manifest_mod.load(args.run_id, root=root)
    index = next((i for i, b in enumerate(run.batches) if b.batch == args.batch), None)
    if index is None:
        names = ", ".join(b.batch for b in run.batches)
        error(f"run {run.run_id} has no batch {args.batch} (it has {names})")
        return FANOUT_PLACE_FAILED
    b = run.batches[index]
    dirs = [args.log_dir] if args.log_dir else None
    found = resumable(run.run_id, b, tools, args.log, dirs)
    if isinstance(found, str):
        error(found)
        return FANOUT_PLACE_FAILED
    registered = registered_keys(tools)
    if isinstance(registered, str):
        error(f"{registered} — refusing to resume")
        return FANOUT_PLACE_NO_SIGNING_HOST
    good = readings(tools, registered, [b.host], root)
    if isinstance(good, int):
        return good
    if placement([b.batch], good, b.host, root, tools.multiplexed()) is None:
        return FANOUT_PLACE_NOTHING_PLACED
    try:
        launch_mod.resume_batch(tools, b, found.request, sizing.by_name(b.rung))
    except launch_mod.LaunchError as exc:
        error(" ".join(f"{b.batch} on {b.host}: {exc}".split()))
        return FANOUT_PLACE_FAILED
    resumed = b.resumed_for_conflict()
    run.batches[index] = resumed
    manifest_mod.save(run, root=root)
    print(
        f"run {run.run_id}: resumed {b.batch} on {b.host} for {found.pr} "
        f"(conflict resume {resumed.conflict_resumes} of {MAX_RESUMES}); "
        f"wait with cc-wait fanout {run.run_id}"
    )
    return FANOUT_PLACE_OK
