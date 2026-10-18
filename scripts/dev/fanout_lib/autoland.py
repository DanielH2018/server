"""Land the PRs of finished daniel-server fan-out batches that are safe to land unattended (#5496).

WHY. A daniel-server batch stops at its PR, because the deploy path lives on daniel-box, and
nothing landed it until an orchestrator ran `land.sh`. Measured over 2026-10-15 to 2026-10-17,
across 58 server batches, the median wait was 5.5 minutes and the 90th percentile 350 minutes.
This command runs on daniel-box from `claude-fanout-server-lander.timer` and starts that
`land.sh` for the PRs that qualify. The operator approved it on 2026-10-18.

WHY NOT `claude-land@`. That unit admits only the `claude` user and pins the author
`DanielClaudeBot` and the branch prefix `worktree-claude+`. A server batch runs as `ubuntu`
on `worktree-fanout-<batch>`, and widening the unit would change the agent lander's policy.

THE ELIGIBILITY RULE has two halves, both in `problems`. The review record that daniel-server
wrote (`~/.local/state/fanout-review/<batch>-<stamp>.json`, read in ONE ssh call because
`ufw limit ssh` rejects 6 connections in 30 seconds) must show a batch that was reviewed, ended
`pr`, passed its CI gate, and left no finding. A `--no-review` batch writes no record, so it
never qualifies: flips are `--no-review` and need operator checks before the merge. The PR
read from GitHub must then still be the one the record reviewed: open, not a draft, on
`worktree-fanout-<batch>`, at the head the record names, with no operator step in its body.

NO PER-PR LOCK. `land.sh` takes none. It holds the deploy tree and service locks, and
`claude-land@` holds a flock, so two landings of one PR would both arm the merge. This command
therefore starts a PR once: it skips a PR whose log directory already holds a `land<pr>-*.log`
(a landing that ended `refused` or `ci-red` is the operator's to read, not to retry every ten
minutes), and a PR that any live `land.py` process names with `--pr <n>`, which finds the
landings an orchestrator started from another log directory.

THE CHECKOUT. Each landing runs from its own worktree at `origin/master` under the state
directory, never from the primary checkout: one untracked file in the primary parks the
deployer, and `deploy.sh` renders from the tree it runs in. The next run removes a worktree
whose landing has ended.

THE UNIT SETS `LAND_REQUIRE_BRANCH_PREFIX` and `LAND_REQUIRE_AUTHOR`. With the prefix set,
`land_lib/policy.py` also refuses while the deployer holds a SHA and pins the merge to the
head it checked, so a push after the checks fails the merge instead of landing unchecked.

DECIDED: pace is 2 landings started per run (#5526: CI demand already exceeds the 20-runner
cap). The rest wait for the next run, which logs them as `eligible, deferred`.

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

# Reach the sibling package: a directly-invoked script gets only its own directory on
# sys.path, and pyproject's `pythonpath` is a pytest setting.
_sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fanout_lib import operator_waits
from fanout_lib.review.api import LAND_SH, login
from fanout_lib.target import SERVER, SERVER_CHECKOUT
from fanout_lib.transport import Tools
from lib.cli_help import ArgumentParser
from lib.gh import gh
from lib.git import git
from lib.detach_fork import alive, rc_path, recorded_pid

SERVER_HOST = "daniel-server"
BRANCH_PREFIX = "worktree-fanout-"
BASE = "master"
# The login every server batch's PR carries. The unit's LAND_REQUIRE_AUTHOR pins the same.
AUTHOR = "DanielH2018"
MAX_STARTS = 2
# Records older than this are not read. A PR that waited longer than two days has a reason.
RECORD_WINDOW_S = 48 * 3600
READ_TIMEOUT_S = 30.0
START_TIMEOUT_S = 600.0
GH_TIMEOUT_S = 60.0
STATE_DIR = Path.home() / ".local" / "state" / "fanout-server-lander"
# gh returns at most this many changed files, so a PR at the cap may hide a path past it.
FILE_CAP = 100
# Paths whose change widens what this command or the lander may do, or that the landing path
# imports. A PR touching one is left for a person, the way `claude-land@`'s approval list leaves
# it. `scripts/deploy_tools/` is the whole tree because land.sh imports await_ci.py and the rest
# of it, not only land*. The deployer's `files/` decide the CI gate, `ansible/vars/` holds the
# secrets and the generated cluster file, and `.claude/` holds the repo's hooks and settings.
OWN_PATHS = (
    "scripts/deploy_tools/",
    "scripts/lib/",
    "scripts/dev/fanout",
    ".github/",
    ".claude/",
    "ansible/roles/setup/claude_code/",
    "ansible/roles/setup/gitops_deploy/",
    "ansible/vars/",
    "pyproject.toml",
    "uv.lock",
    "uv.toml",
    ".python-version",
)
# A heading that names a step only a person can do, and the text that holds a PR back.
OPERATOR_HEADING = re.compile(r"(?i)before[- ]merge|operator")
HOLD_TEXT = re.compile(r"(?i)needs input|do not merge")
HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*)")
UNCHECKED = re.compile(r"^\s*[-*]\s*\[ \]")
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
        owed: the deployer's `owed.jsonl`, for the waiting gauge.
        clock: the epoch second now.
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
    owed: Callable[[], str] = lambda: operator_waits.read_owed()
    clock: Callable[[], float] = time.time


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


def operator_step_in(body: str) -> str:
    """Why the PR body holds the PR back, or "" when it does not.

    An unchecked box under a heading that names an operator or before-merge step, or text
    saying the PR needs input or must not merge. The template's own unchecked deploy boxes sit
    under other headings and do not count.
    """
    heading = ""
    for line in body.splitlines():
        found = HEADING.match(line)
        if found:
            heading = found.group(1).strip()
        elif heading and OPERATOR_HEADING.search(heading) and UNCHECKED.match(line):
            return f"an unchecked box under '{heading}'"
    hold = HOLD_TEXT.search(body)
    return f"the body says '{hold.group(0)}'" if hold else ""


def problems(rec: dict, pr: dict) -> str:
    """Why this PR may not land unattended, or "" when it may. First reason wins.

    `rec` is the batch's review record, `pr` the PR as GitHub reads it now.
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
    if pr.get("isCrossRepository") or pr.get("baseRefName") != BASE:
        return f"the PR does not target {BASE} from this repo"
    if pr.get("headRefName") != f"{BRANCH_PREFIX}{rec.get('batch')}":
        return f"the head branch is '{pr.get('headRefName')}', not the batch's"
    if (pr.get("author") or {}).get("login") != AUTHOR:
        return f"the author is not {AUTHOR}"
    head = str(pr.get("headRefOid") or "")
    if head != reviewed:
        return f"the head moved since the review ({reviewed[:8]} -> {head[:8]})"
    files = [str(f.get("path") or "") for f in pr.get("files") or []]
    if len(files) >= FILE_CAP:
        return f"it changes {len(files)} files, at the listing cap"
    own = [f for f in files if f.startswith(OWN_PATHS)]
    if own:
        return f"it changes the lander's own surface: {', '.join(own[:3])}"
    return operator_step_in(str(pr.get("body") or ""))


def handed_off(log_dir: Path, pr: int) -> str:
    """The log of an earlier landing of this PR that this command started, or ""."""
    logs = sorted(log_dir.glob(f"land{pr}-*.log")) if log_dir.is_dir() else []
    return logs[-1].name if logs else ""


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
        ["fetch", "--quiet", "origin", BASE],
        ["worktree", "add", "--detach", str(path), f"origin/{BASE}"],
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


def run(
    b: Boundaries,
    primary: Path = Path(SERVER_CHECKOUT),
    state: Path = STATE_DIR,
    dry_run: bool = False,
    out: Callable[[str], None] = print,
    gauge: Path | None = None,
) -> int:
    """One pass: read, judge every candidate PR, start at most `MAX_STARTS` landings.

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
    started = 0
    for number in sorted(candidates):
        rec, label = (
            candidates[number],
            f"#{number} batch {candidates[number].get('batch')}",
        )
        why = problems(rec, prs[number])
        if why:
            out(f"{label}: ineligible, {why}")
        elif log := handed_off(log_dir, number):
            out(f"{label}: ineligible, already handed off ({log})")
        elif b.running(number):
            out(f"{label}: ineligible, a landing of it is already running")
        elif started >= MAX_STARTS:
            out(
                f"{label}: eligible, deferred to the next run (pace {MAX_STARTS} a run)"
            )
        elif dry_run:
            out(f"{label}: eligible, would hand off to land.sh (dry run)")
        else:
            started += 1
            hand_off(b, primary, state, log_dir, number, label, out)
    if gauge is not None and not dry_run:
        write_waits(b, candidates, prs, gauge, out)
    return 0


def write_waits(
    b: Boundaries,
    server: dict[int, dict],
    prs: dict[int, dict],
    gauge: Path,
    out: Callable[[str], None],
) -> None:
    """Write the gauge of what waits on a person, from this pass's reads and two local ones."""
    box = {n: r for n, r in newest_per_pr(b.local_records()).items() if n in prs}
    starts = operator_waits.waits(
        [(problems(r, prs[n]), r) for n, r in server.items()],
        [(problems(r, prs[n]), r) for n, r in box.items()],
        operator_waits.owed_manual_planes(b.owed()),
    )
    try:
        wrote = operator_waits.write(gauge, operator_waits.render(starts, b.clock()))
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
    """Start one landing. A failure is logged with its reason and leaves the PR untouched."""
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
        out(
            f"{label}: eligible, handoff failed (exit {done.returncode}): "
            f"{tail(done.stdout + done.stderr)}"
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
    return run(
        Boundaries(),
        args.primary,
        args.state_dir,
        args.dry_run,
        gauge=operator_waits.TEXTFILE,
    )


if __name__ == "__main__":
    _sys.exit(main())
