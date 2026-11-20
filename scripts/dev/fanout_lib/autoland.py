"""Land the PRs of finished daniel-server fan-out batches that are safe to land unattended.

WHY. A daniel-server batch stops at its PR, because the deploy path lives on daniel-box, and
nothing landed it until an orchestrator ran `land.sh`. Measured over 2026-10-15 to 2026-10-17,
across 58 server batches, the median wait was 5.5 minutes and the 90th percentile 350 minutes.
This command runs on daniel-box from `homelab-claude-fanout-server-lander.timer` and starts that
`land.sh` for the PRs that qualify. The operator approved it on 2026-10-18.

WHY NOT `homelab-claude-land@`. That unit admits only the `claude` user and pins the author
`claudemachineacct1` and the branch prefix `worktree-claude+`. A server batch runs as `ubuntu`
on `worktree-fanout-<batch>`, and widening the unit would change the agent lander's policy.

THE ELIGIBILITY RULE has two halves, both in `problems`. The review record that daniel-server
wrote (`~/.local/state/fanout-review/<batch>-<stamp>.json`, read in ONE ssh call because
`ufw limit ssh` rejects 6 connections in 30 seconds) must show a batch that was reviewed, ended
`pr`, passed its CI gate, and left no finding. A `--no-review` batch writes no record, so it
never qualifies: flips are `--no-review` and need operator checks before the merge. The PR
read from GitHub must then still be the one the record reviewed: open, not a draft, on
`worktree-fanout-<batch>`, at the head the record names, with no operator step in its body.

A PR IS STARTED ONCE. `land.sh` refuses a second landing of a PR while one is live, on
either host (`land_lib/pr_lock.py`), and a start it refuses that way is logged as `another
landing has it`, never as a failed handoff. The lock ends with its landing, so this command
keeps its own rule for a landing that has ended: it skips a PR whose log directory already
holds a `land<pr>-*.log` (a landing that ended `refused` or `ci-red` is the operator's to
read, not to retry every ten minutes, unless a rule refused it before its merge with exit
75). It also skips a PR that any live `land.py` process on this host names with `--pr <n>`,
which spares the checkout a refused start would make and remove.

THE CHECKOUT. Each landing runs from its own worktree at `origin/master` under the state
directory, never from the primary checkout: one untracked file in the primary parks
`homelab-checkout-sync`, and `land.sh` runs `land.py` and its helpers from the checkout it
lives in (`land_lib/own_checkout.py`). The next run removes a worktree whose landing has ended.

THE UNIT SETS `LAND_REQUIRE_BRANCH_PREFIX` and `LAND_REQUIRE_AUTHOR`. With the prefix set,
`land_lib/policy.py` also pins the merge to the head it checked, so a push after the checks
fails the merge instead of landing unchecked.

NO START CAP. A run starts every eligible PR. Before its merge a landing waits for the landing
turn, and only the holder runs its PR's CI; after the merge it waits for a landing slot. Those
two order and bound the landings, so a cap here would only delay a PR by a run.

THE WORKTREE SWEEP. Nothing else cleans a landed batch's worktree unless an operator
runs `place clean`, so a pass also runs `clean.sweep_runs` over this user's open run manifests,
at most every `CLEAN_INTERVAL_S`. Its legs log lines and never change the exit status.

THE COORDINATOR CHECK. Each pass ends with `coordinators.report`, which pages the operator
for a coordinator project whose session is gone while a batch waits on it. It logs a line per
change and never changes the exit status either.

DECIDED: a daniel-server that cannot be read is a logged line and exit 0, not a failure. The
host is off for the weekly reboot and for maintenance, and a page every ten minutes for that
would teach the operator to ignore the page. A failed GitHub read exits 1, because that one
means this host's own credentials broke.
"""

import json
import re
import shutil
import subprocess
import sys as _sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path


from dev.findings_lib.boundaries import FindingsTools
from dev.findings_lib.gh_calls import load_issues
from dev.findings_lib.issue_model import Issue
from dev.fanout_lib import coordinators, operator_waits, own_surface
from deploy_tools.land_lib.detach import result_fields
from dev.fanout_lib.clean import sweep_runs
from dev.fanout_lib.land_result import another_has_it
from dev.fanout_lib.launch_gates import LOCAL_HOST, live_batch_cap
from dev.fanout_lib.review.api import LAND_SH, VERDICT_LINE, login
from dev.fanout_lib.target import SERVER, SERVER_CHECKOUT
from dev.fanout_lib.transport import Tools
from lib.cli_help import ArgumentParser
from lib.gh import gh
from lib.git import git
from lib.detach_fork import alive, rc_path, recorded_pid

SERVER_HOST = "daniel-server"
# Records older than this are not read. A PR that waited longer than two days has a reason.
RECORD_WINDOW_S = 48 * 3600
READ_TIMEOUT_S = 30.0
START_TIMEOUT_S = 600.0
GH_TIMEOUT_S = 60.0
STATE_DIR = Path.home() / ".local" / "state" / "fanout-server-lander"
# The worktree sweep's stamp in STATE_DIR, and how often it runs: a landed batch's tree is gone
# within a day, and the legs of the batches that stay kept are not re-run every ten minutes.
CLEAN_STAMP = "clean-sweep.stamp"
CLEAN_INTERVAL_S = 6 * 3600
PR_FIELDS = (
    "number,isDraft,headRefName,headRefOid,baseRefName,body,author,files,"
    "isCrossRepository"
)

# Runs on daniel-server with `python3 -I -`, so nothing needs shell quoting. It prints the
# fields the rule reads from each recent record, and the count of findings left.
READER = f"""\
import json, pathlib, time
d = pathlib.Path.home() / ".local" / "state" / "fanout-review"
cut = time.time() - {RECORD_WINDOW_S}
out = []
for p in sorted(d.glob("*.json")):
    try:
        if p.stat().st_mtime < cut:
            continue
        r = json.loads(p.read_text())
    except (OSError, ValueError):
        continue
    if not isinstance(r, dict):
        continue
    out.append({{
        "file": p.name,
        "batch": r.get("batch"),
        "pr": r.get("pr"),
        "outcome": r.get("outcome"),
        "review_error": r.get("review_error"),
        "reviewed": "review" in (r.get("costs") or {{}}),
        "ci_gate": r.get("ci_gate"),
        "remaining": len(r.get("remaining") or []),
        "head": r.get("head"),
    }})
print(json.dumps(out))
"""


@dataclass
class Boundaries:
    """Every process boundary this command crosses, so a test replaces one field.

    Attributes:
        tools: the fan-out transport; `tools.run` is the one ssh call to daniel-server.
        open_prs: the open PRs of the repo, as `gh pr list --json PR_FIELDS` decodes them.
        prepare: make the PR's checkout at origin/master; its path, or raise `CheckoutFailed`.
        start: run the landing's start command in a checkout, with its log directory.
        release: remove a checkout.
        running: whether a live `land.py` names this PR.
        local_records: daniel-box's own recent review records, for the waiting gauge.
        clock: the epoch second now.
        testpaths_only: whether the PR at this head changes `pyproject.toml` only in `testpaths`.
        clean: clean every landed batch's worktree, given the branches of the open PRs.
    """

    tools: Tools = field(default_factory=Tools)
    open_prs: Callable[[], list[dict]] = lambda: read_open_prs()
    prepare: Callable[[Path, Path, int], Path] = lambda primary, state, pr: (
        prepare_checkout(primary, state, pr)
    )
    start: Callable[[Path, int, Path], subprocess.CompletedProcess] = lambda *a: (
        start_landing(*a)
    )
    release: Callable[[Path, Path], None] = lambda primary, path: release_checkout(
        primary, path
    )
    running: Callable[[int], bool] = lambda pr: landing_running(pr)
    local_records: Callable[[], list[dict]] = lambda: operator_waits.read_local_records(
        READER
    )
    open_issues: Callable[[], list[Issue]] = lambda: read_open_issues()
    clock: Callable[[], float] = time.time
    testpaths_only: Callable[[str], bool] = lambda head: (
        own_surface.live_testpaths_only(head)
    )
    clean: Callable[[Tools, frozenset[str], Callable[[str], None]], None] = (
        lambda tools, open_branches, out: sweep_runs(tools, open_branches, out)
    )

    def judge(self, rec: dict, pr: dict) -> str:
        """`problems` with this boundary's diff read."""
        return problems(rec, pr, self.testpaths_only)


class CheckoutFailed(Exception):
    """The landing's checkout could not be made."""


def read_open_prs() -> list[dict]:
    """Every open PR of the repo with the fields `problems` reads; raises on a failed read."""
    out = gh(
        "pr", "list", "--repo", SERVER, "--state", "open", "--limit", "200",
        "--json", PR_FIELDS, timeout=GH_TIMEOUT_S,
    )  # fmt: skip
    data = json.loads(out.stdout)
    if not isinstance(data, list):
        raise ValueError("gh pr list did not print a list")
    return [p for p in data if isinstance(p, dict)]


def read_open_issues() -> list[Issue]:
    """Every open register issue as `findings.py` loads it, for the queue gauge; raises on a failed read."""
    return load_issues("open", FindingsTools())


def newest_per_pr(records: Sequence[dict]) -> dict[int, dict]:
    """The newest record for each PR number, by the stamp in its file name."""
    newest: dict[int, dict] = {}
    for rec in records:
        match = re.search(r"/pull/(\d+)\s*$", str(rec.get("pr") or ""))
        if not match:
            continue
        number = int(match.group(1))
        stamp = str(rec.get("file") or "").rsplit("-", 1)[-1]
        old = newest.get(number)
        if old is None or stamp >= str(old.get("file") or "").rsplit("-", 1)[-1]:
            newest[number] = rec
    return newest


def problems(
    rec: dict,
    pr: dict,
    pyproject_ok: Callable[[str], bool] = own_surface.never,
) -> str:
    """Why this PR may not land unattended, or "" when it may. First reason wins.

    `rec` is the batch's review record, `pr` the PR as GitHub reads it now. `pyproject_ok`
    answers `Boundaries.testpaths_only`. The default holds every PR that touches
    `pyproject.toml`.
    """
    if rec.get("outcome") != "pr":
        return f"the batch ended '{rec.get('outcome')}', not 'pr'"
    if not rec.get("reviewed") or rec.get("review_error"):
        return f"the review did not complete ({rec.get('review_error') or 'no review phase'})"
    if not str(rec.get("ci_gate") or "").startswith("passed"):
        return f"the CI gate did not pass ({rec.get('ci_gate') or 'it did not run'})"
    if rec.get("remaining"):
        return f"{rec['remaining']} findings remain after the fix round"
    reviewed = str(rec.get("head") or "")
    if not reviewed:
        return "the record names no reviewed head (written before the field existed)"
    if pr.get("isDraft"):
        return "the PR is a draft"
    head = str(pr.get("headRefOid") or "")
    if head != reviewed:
        return f"the head moved since the review ({reviewed[:8]} -> {head[:8]})"
    return own_surface.pr_problems(pr, str(rec.get("batch")), pyproject_ok)


# A landing that a rule refused before its merge, which merged nothing and ends exit 75
# (`land_lib/slots.py:refuse_before_merge`).
_REFUSED_FOR_NOW = re.compile(
    r"^LAND-RESULT: verdict=refused .*\bexit=75\b", re.MULTILINE
)


def _logs(log_dir: Path, pr: int) -> list[Path]:
    return sorted(log_dir.glob(f"land{pr}-*.log")) if log_dir.is_dir() else []


def refused_for_now(log_dir: Path, pr: int) -> bool:
    """Whether a rule refused this PR's newest landing before its merge."""
    logs = _logs(log_dir, pr)
    try:
        return bool(logs) and bool(
            _REFUSED_FOR_NOW.search(logs[-1].read_text(errors="replace"))
        )
    except OSError:
        return False


def handed_off(log_dir: Path, pr: int) -> str:
    """The log of an earlier landing of this PR that this command started, or "".

    A PR `refused_for_now` reads as none, so a run starts it again once the rule clears.
    """
    logs = _logs(log_dir, pr)
    return logs[-1].name if logs and not refused_for_now(log_dir, pr) else ""


def landing_running(pr: int, proc: Path = Path("/proc")) -> bool:
    """Whether a live process runs `land.py`/`land.sh` with the exact tokens `--pr <pr>`.

    Whole tokens, never a substring: `--pr 55` must not match `--pr 5501`.
    """
    want = str(pr).encode()
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if not any(a.endswith((b"land.py", b"land.sh")) for a in argv):
            continue
        if any(
            a == b"--pr" and b == want for a, b in zip(argv, argv[1:], strict=False)
        ) or (b"--pr=" + want in argv):
            return True
    return False


def checkout_path(state: Path, pr: int) -> Path:
    return state / "checkouts" / f"pr{pr}"


def prepare_checkout(primary: Path, state: Path, pr: int) -> Path:
    """A fresh detached worktree of the primary's `origin/master`, one per landing."""
    path = checkout_path(state, pr)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        release_checkout(primary, path)
    for argv in (
        ["fetch", "--quiet", "origin", own_surface.BASE],
        ["worktree", "add", "--detach", str(path), f"origin/{own_surface.BASE}"],
    ):
        done = git(*argv, cwd=primary, check=False, timeout=300)
        if done.returncode != 0:
            raise CheckoutFailed(" ".join((done.stdout + done.stderr).split())[-300:])
    return path


def release_checkout(primary: Path, path: Path) -> None:
    """Remove a landing's worktree; a leftover directory git no longer knows is deleted."""
    git(
        "worktree",
        "remove",
        "--force",
        str(path),
        cwd=primary,
        check=False,
        timeout=120,
    )
    shutil.rmtree(path, ignore_errors=True)
    git("worktree", "prune", cwd=primary, check=False, timeout=120)


def start_landing(
    checkout: Path, pr: int, log_dir: Path
) -> subprocess.CompletedProcess:
    """Run `land.sh --detach` from `checkout`; it returns once the landing has forked."""
    argv = [
        str(checkout / LAND_SH), "--pr", str(pr), "--arm-merge", "--await-merge",
        "--detach", "--log-dir", str(log_dir),
    ]  # fmt: skip
    return subprocess.run(
        login(argv),
        capture_output=True,
        text=True,
        timeout=START_TIMEOUT_S,
        check=False,
    )


def finished(log_dir: Path, pr: int) -> bool:
    """Whether every landing log of this PR in `log_dir` has ended."""
    for log in log_dir.glob(f"land{pr}-*.log"):
        pid = recorded_pid(log)
        if not rc_path(log).exists() and pid is not None and alive(pid):
            return False
    return True


def sweep(primary: Path, state: Path, log_dir: Path, release: Callable) -> None:
    """Remove the checkout of every landing that has ended."""
    root = state / "checkouts"
    for path in sorted(root.glob("pr*")) if root.is_dir() else []:
        digits = path.name.removeprefix("pr")
        if digits.isdigit() and finished(log_dir, int(digits)):
            release(primary, path)


def tail(text: str) -> str:
    return " ".join(text.split())[-300:]


_LAND_LINE = re.compile(r"(?m)^land: .*$")


def refusal(done: subprocess.CompletedProcess) -> str:
    """Why a `land.sh` start failed, from its stdout and stderr.

    Its last `VERDICT:` line, else its last `land:` line, else the output's `tail`. uv's
    virtual-environment chatter comes first in a fresh checkout, so a plain tail cut the
    deciding line short.
    """
    output = f"{done.stdout or ''}\n{done.stderr or ''}"
    found = list(VERDICT_LINE.finditer(output)) or list(_LAND_LINE.finditer(output))
    return " ".join(found[-1].group(0).split())[:300] if found else tail(output)


def run(
    b: Boundaries,
    primary: Path = Path(SERVER_CHECKOUT),
    state: Path = STATE_DIR,
    dry_run: bool = False,
    out: Callable[[str], None] = print,
    gauge: Path | None = None,
) -> int:
    """One pass: read, judge every candidate PR, start a landing for each eligible one.

    Prints one line per candidate PR. Returns 0, or 1 when GitHub could not be read. With
    `gauge`, a pass that read both sources also writes the waiting gauge there
    (`fanout_lib/operator_waits.py`); a dry run writes nothing.
    """
    log_dir = state / "logs"
    try:
        proc = b.tools.run(SERVER_HOST, "python3 -I -", READ_TIMEOUT_S, READER)
        if proc.returncode != 0:
            raise OSError(f"exit {proc.returncode}: {tail(proc.stderr)}")
        records = json.loads(proc.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        out(f"{SERVER_HOST} records unreadable, nothing landed: {tail(str(exc))}")
        return 0
    try:
        prs = {int(p["number"]): p for p in b.open_prs()}
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        out(f"GitHub unreadable, nothing landed: {tail(str(exc))}")
        return 1
    if not dry_run:
        sweep(primary, state, log_dir, b.release)
    newest = newest_per_pr(records if isinstance(records, list) else [])
    candidates = {n: r for n, r in newest.items() if n in prs}
    out(f"{len(newest)} PRs in the last records, {len(candidates)} still open")
    for number in sorted(candidates):
        rec, label = (
            candidates[number],
            f"#{number} batch {candidates[number].get('batch')}",
        )
        why = b.judge(rec, prs[number])
        if why:
            out(f"{label}: ineligible, {why}")
        elif log := handed_off(log_dir, number):
            out(f"{label}: ineligible, already handed off ({log})")
        elif b.running(number):
            out(f"{label}: ineligible, a landing of it is already running")
        elif dry_run:
            out(f"{label}: eligible, would hand off to land.sh (dry run)")
        else:
            hand_off(b, primary, state, log_dir, number, label, out)
    if gauge is not None and not dry_run:
        write_waits(b, candidates, prs, gauge, out, state / operator_waits.STAMPS_FILE)
    if not dry_run:
        clean_landed(b, state, prs, out)
    return 0


def clean_landed(
    b: Boundaries, state: Path, prs: dict[int, dict], out: Callable[[str], None]
) -> None:
    """Run the worktree sweep (`clean.sweep_runs`) when the last one is `CLEAN_INTERVAL_S` old.

    The stamp is written before the sweep, so a sweep that dies is not retried every pass.
    A stamp that cannot be read reads as no stamp, and one that cannot be written skips the
    sweep with a line: a sweep with no stamp behind it would run every ten minutes.
    """
    stamp = state / CLEAN_STAMP
    now = b.clock()
    try:
        last = float(stamp.read_text())
    except OSError, ValueError:
        last = 0.0
    if now - last < CLEAN_INTERVAL_S:
        return
    try:
        state.mkdir(parents=True, exist_ok=True)
        stamp.write_text(f"{now}\n")
    except OSError as exc:
        out(f"clean sweep skipped, stamp not written: {tail(str(exc))}")
        return
    b.clean(b.tools, frozenset(p.get("headRefName", "") for p in prs.values()), out)


def write_waits(
    b: Boundaries,
    server: dict[int, dict],
    prs: dict[int, dict],
    gauge: Path,
    out: Callable[[str], None],
    stamps: Path | None = None,
) -> None:
    """Write the gauge of what waits on a person, from this pass's reads and two local ones.

    With `stamps`, each PR held for a counted reason is also stamped there.
    """
    box = {n: r for n, r in newest_per_pr(b.local_records()).items() if n in prs}
    if stamps is not None:
        held = {
            n: reason
            for host, recs in (("box", box), ("server", server))
            for n, r in recs.items()
            if (reason := operator_waits.counted_reason(host, b.judge(r, prs[n])))
        }
        try:
            operator_waits.record_stamps(stamps, held, b.clock())
        except OSError as exc:
            out(f"operator wait stamps not written: {tail(str(exc))}")
    starts = operator_waits.waits(
        [(b.judge(r, prs[n]), r) for n, r in server.items()],
        [(b.judge(r, prs[n]), r) for n, r in box.items()],
    )
    body = operator_waits.render(starts, b.clock())
    try:
        today = time.strftime("%Y-%m-%d", time.gmtime(b.clock()))
        states = operator_waits.issue_states(b.open_issues(), today)
        capacity = {h: live_batch_cap(h) for h in (LOCAL_HOST, SERVER_HOST)}
        body += operator_waits.render_queue(states, capacity)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        # The waits still stand; the queue families stay absent rather than reading zero.
        out(f"issue queue not read: {tail(str(exc))}")
    try:
        wrote = operator_waits.write(gauge, body)
    except OSError as exc:
        out(f"waiting gauge not written: {tail(str(exc))}")
        return
    if not wrote:
        out(f"waiting gauge not written: {gauge.parent} is missing")


def hand_off(
    b: Boundaries,
    primary: Path,
    state: Path,
    log_dir: Path,
    number: int,
    label: str,
    out: Callable[[str], None],
) -> None:
    """Start one landing. A failure is logged with its reason and leaves the PR untouched.

    A start `land.sh` refused because another landing of the PR is live is logged as that,
    and the next pass judges the PR again.
    """
    try:
        checkout = b.prepare(primary, state, number)
    except (CheckoutFailed, OSError, subprocess.SubprocessError) as exc:
        out(f"{label}: eligible, handoff failed, no checkout: {tail(str(exc))}")
        return
    try:
        done = b.start(checkout, number, log_dir)
    except (OSError, subprocess.SubprocessError) as exc:
        out(f"{label}: eligible, handoff failed: {tail(str(exc))}")
        b.release(primary, checkout)
        return
    if done.returncode != 0:
        if another_has_it(result_fields(done.stdout or "") or {}):
            out(f"{label}: eligible, another landing has it: {refusal(done)}")
        else:
            out(
                f"{label}: eligible, handoff failed (exit {done.returncode}): "
                f"{refusal(done)}"
            )
        b.release(primary, checkout)
        return
    out(f"{label}: eligible, handed off to land.sh; log in {log_dir}")


def main(argv: list[str] | None = None) -> int:
    p = ArgumentParser(
        prog="fanout.py land-server", description=__doc__.splitlines()[0]
    )
    p.add_argument(
        "--dry-run", action="store_true", help="judge and log, start nothing"
    )
    p.add_argument("--primary", type=Path, default=Path(SERVER_CHECKOUT))
    p.add_argument("--state-dir", type=Path, default=STATE_DIR)
    args = p.parse_args(argv)
    code = run(
        Boundaries(),
        args.primary,
        args.state_dir,
        args.dry_run,
        gauge=operator_waits.TEXTFILE,
    )
    # After the pass and whatever it returned: the check reads this host's worktrees and
    # run manifests, so it still runs when daniel-server or GitHub could not be read.
    coordinators.report(args.state_dir, dry_run=args.dry_run)
    return code


if __name__ == "__main__":
    _sys.exit(main())
