"""Readers behind the three panels of the deploy UI. Pure parsers, stdlib only.

The daemon runs under `uv run --no-project` on the host interpreter, outside the repo venv,
so nothing here imports from `scripts/`. `ps`, `fuser` and `gh` are reached as subprocesses
by `deploy_ui.App`; this module turns their text into rows.
"""

import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

_RUN_RE = re.compile(r"\bland\.py\b")
_PR_RE = re.compile(r"--pr\s+(\d+)")
_TAGS_RE = re.compile(r"--tags[= ]+(\S+)")
# `grep` is whoever is looking for a run, not a run.
_NOT_A_RUN = re.compile(r"^grep\b")


@dataclass(frozen=True)
class Proc:
    pid: int
    ppid: int
    elapsed_s: int
    args: str


@dataclass(frozen=True)
class Run:
    """One process family in flight: a landing or a bare lock holder.

    `locks` names the lock files the family holds and `waiting_on` the ones it is queued
    on, both by basename. A process blocked on a lock has the file OPEN for the whole
    wait and fuser lists it like the holder; only the kernel's lock table (`/proc/locks`)
    tells the two apart.
    """

    pid: int
    elapsed_s: int
    kind: str
    pr: str
    tag: str
    args: str
    locks: tuple[str, ...]
    waiting_on: tuple[str, ...]
    log: str = ""


@dataclass(frozen=True)
class FileLock:
    """One lock file's state from `/proc/locks`: granted or not, and who is blocked on it."""

    held: bool
    waiters: frozenset[int]


# `56: FLOCK  ADVISORY  WRITE 1930784 fc:00:6564011 0 EOF` is a granted flock;
# `56: -> FLOCK …` under it is a process blocked on the same lock. The pid on a granted
# line can be dead: a lock taken by a `flock` CHILD on an inherited descriptor outlives the
# child. The pid on a `->` line is alive by construction, blocked inside flock(2).
_PROC_LOCK_RE = re.compile(
    r"^\d+:\s*(->\s*)?FLOCK\s+\S+\s+(?:READ|WRITE)\s+(\d+)\s+"
    r"([0-9a-f]+):([0-9a-f]+):(\d+)\s"
)

LockKey = tuple[int, int, int]


def lock_key(path: str) -> LockKey:
    """`(major, minor, inode)`, the identity `/proc/locks` names a file by."""
    st = os.stat(path)
    return (os.major(st.st_dev), os.minor(st.st_dev), st.st_ino)


def parse_proc_locks(text: str) -> dict[LockKey, FileLock]:
    """`/proc/locks` to the flocks in it, keyed the way `lock_key` keys a path."""
    held: set[LockKey] = set()
    waiters: dict[LockKey, set[int]] = {}
    for line in text.splitlines():
        m = _PROC_LOCK_RE.match(line)
        if not m:
            continue
        key = (int(m.group(3), 16), int(m.group(4), 16), int(m.group(5)))
        if m.group(1):
            waiters.setdefault(key, set()).add(int(m.group(2)))
        else:
            held.add(key)
    return {
        key: FileLock(key in held, frozenset(waiters.get(key, ())))
        for key in held | waiters.keys()
    }


def parse_ps(text: str) -> dict[int, Proc]:
    """`ps -eo pid=,ppid=,etimes=,args=` lines to every process, keyed by pid."""
    procs = {}
    for line in text.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4 or not (parts[0].isdigit() and parts[1].isdigit()):
            continue
        pid, ppid, etimes, args = int(parts[0]), int(parts[1]), int(parts[2]), parts[3]
        procs[pid] = Proc(pid, ppid, etimes, args)
    return procs


def parse_fuser(text: str) -> dict[str, set[int]]:
    """`fuser <paths…>` with stderr merged into stdout: `<path>: <pid> <pid>` per HELD path.

    fuser writes the path to stderr and the pids to stdout, one line per path that has a
    holder, and nothing for a path that has none. psmisc flushes between them, so the
    merged stream keeps the pairing (measured on psmisc 23.7 with two of three files held).
    """
    held: dict[str, set[int]] = {}
    for line in text.splitlines():
        path, sep, pids = line.partition(":")
        if not sep:
            continue
        found = {int(t) for t in pids.split() if t.isdigit()}
        if found:
            held[path.strip()] = found
    return held


def _is_run(args: str) -> bool:
    return bool(_RUN_RE.search(args)) and not _NOT_A_RUN.search(args)


def runs(
    procs: dict[int, Proc],
    opened: dict[str, set[int]],
    locks: dict[str, FileLock],
) -> list[Run]:
    """Fold processes into one row per family.

    The topmost run or lock holder on a pid's ancestor chain owns every run and holder
    below it. A landing is `uv run … land.py`, its python child, and every process it
    spawns: one row, kind `land`. A lock holder that is not a landing — the GitOps tick
    on the tree lock — is a row of kind `lock`,
    so a held lock is never invisible because its holder matched no pattern. fuser lists
    every process with the file open, children included, which is why the fold has to
    include holders and not only runs.

    `opened` is fuser's answer per lock path; `locks` is `/proc/locks` per lock path. A
    family holds a lock when it has the file open, the kernel has granted a lock on it,
    and no member of the family is blocked on it. A family with a member blocked on it
    is waiting.
    """
    open_pids = set().union(*opened.values()) if opened else set()
    waiter_pids = set().union(*(l.waiters for l in locks.values())) if locks else set()
    candidates = {pid for pid, p in procs.items() if _is_run(p.args)} | (
        (open_pids | waiter_pids) & procs.keys()
    )
    families: dict[int, set[int]] = {}
    for pid in candidates:
        root, cur, seen = pid, pid, set()
        while cur in procs and cur not in seen:
            seen.add(cur)
            if cur in candidates:
                root = cur
            cur = procs[cur].ppid
        families.setdefault(root, set()).add(pid)
    rows = []
    for root, members in families.items():
        p = procs[root]
        holding, waiting = [], []
        for path, pids in opened.items():
            if not pids & members:
                continue
            lock = locks.get(path)
            name = os.path.basename(path)
            if lock and lock.waiters & members:
                waiting.append(name)
            elif lock and lock.held:
                holding.append(name)
        kind = "land" if _is_run(p.args) else "lock"
        pr = _PR_RE.search(p.args)
        tag = _TAGS_RE.search(p.args)
        rows.append(
            Run(
                p.pid,
                p.elapsed_s,
                kind,
                pr.group(1) if pr else "",
                tag.group(1) if tag else "",
                p.args,
                tuple(sorted(holding)),
                tuple(sorted(waiting)),
            )
        )
    return sorted(rows, key=lambda r: r.pid)


def log_path_of(pid: int) -> str:
    """Where the process's stdout goes, or '' when unreadable."""
    try:
        return os.readlink(f"/proc/{pid}/fd/1")
    except OSError:
        return ""


def _ci_of(rollup: list[dict]) -> str:
    if not rollup:
        return "none"
    concl = {(c.get("conclusion") or "").upper() for c in rollup}
    if concl & {"FAILURE", "ERROR", "TIMED_OUT"}:
        return "fail"
    if any(
        (c.get("status") or "").upper() in ("IN_PROGRESS", "QUEUED", "PENDING")
        for c in rollup
    ):
        return "pending"
    if concl and concl <= {"SUCCESS", "SKIPPED", "NEUTRAL"}:
        return "pass"
    return "pending"


def parse_prs(raw: str) -> list[dict]:
    """`gh pr list --json number,title,headRefName,isDraft,statusCheckRollup` to rows."""
    return [
        {
            "number": p["number"],
            "title": p.get("title", ""),
            "branch": p.get("headRefName", ""),
            "draft": bool(p.get("isDraft")),
            "ci": _ci_of(p.get("statusCheckRollup") or []),
        }
        for p in json.loads(raw)
    ]


# A landing's log: `land<pr>-<YYYYmmdd-HHMMSS>.log` from `land.sh --detach`, or
# `land-<YYYYmmddTHHMMSSZ>-<rand>.log` from the daemon's own `spawn_logged`.
LANDING_LOG = re.compile(r"land\d*-\d{8}[-T]\d{6}Z?(-\w+)?\.log")
# The most of one log the viewer reads: the first stage headers matter more than the tail.
MAX_LOG_BYTES = 1_000_000
_STAGE = re.compile(r"^== (?:(\d+)/(\d+)|arm)\s+(.*)$")
_VERDICT_LINE = re.compile(r"^VERDICT: (\S+)(?: \((.*)\))?\s*$")
_PR_IN_LOG = re.compile(r"PR #(\d+)")

# Every verdict `land_lib/outcome.py:Verdict` prints, with a tone for the banner and what to
# do next. The daemon cannot import land_lib; a test pins these keys to `VERDICTS`.
VERDICT_HINTS: dict[str, tuple[str, str]] = {
    "settled": (
        "ok",
        "Deployed and settled. Exercise the change itself: the gate sees the workload, "
        "not your change.",
    ),
    "nothing-to-deploy": (
        "none",
        "Nothing deployed. A PR that reaches only a setup role is applied by the "
        "deployer's timer, which this landing does not watch.",
    ),
    "unhealthy": (
        "bad",
        "Deployed, but a changed workload failed its health gate. The lines above name it.",
    ),
    "deploy-failed": (
        "bad",
        "The deploy failed. Cause promote-held is the operator's DEPLOY_HOLD to clear. "
        "The verdict line names any other cause.",
    ),
    "needs-manual-apply": (
        "warn",
        "Landed but not live. Read every remediation line above, not only the first.",
    ),
    "deferred": (
        "warn",
        "The landing stopped waiting. A host's updater applies its package on its own "
        "timer; for anything else, re-run land.sh to grade the deploy.",
    ),
    "merge-conflict": ("bad", "The PR conflicts with master. Rebase, push and re-run."),
    "pr-ci-red": ("bad", "The PR's own CI is red. Fix it, push and re-run."),
    "merge-timeout": (
        "warn",
        "The merge budget ran out. Nothing deployed; re-run the same command.",
    ),
    "ci-red": (
        "bad",
        "Master CI is red on the merge commit. Nothing deployed; re-run once it is green.",
    ),
    "ci-timeout": (
        "warn",
        "Master CI did not finish in time. Nothing deployed; re-run the same command.",
    ),
    "ci-unproven": (
        "bad",
        "GitHub never ran CI for the merge commit, so a re-run alone never converges. "
        "The detail names the way out.",
    ),
    "refused": (
        "bad",
        "The landing stopped before deploying anything. The detail says why; fix that and "
        "re-run.",
    ),
    "handoff-timeout": (
        "warn",
        "The lander unit may still be running. Re-run the same command; it waits on the "
        "unit again.",
    ),
    "unbuilt": (
        "bad",
        "No package of a host at or after the merge, and the builder reports why. Fix the "
        "builder (probe.py builder), then re-run.",
    ),
}


def landing_log(path: Path, window_s: float, now: float) -> bool:
    """True when `path` is a regular file named like a landing log, written in the window.

    lstat, not stat: a symlink is refused rather than followed, wherever it points.
    """
    if not LANDING_LOG.fullmatch(path.name):
        return False
    try:
        st = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and now - st.st_mtime <= window_s


def landing_index(
    dirs: list[Path], seen: list[Path], window_s: float, now: float
) -> dict[str, Path]:
    """Every landing log in `dirs` or `seen` written in the window, by file name.

    `seen` holds the logs of landings the in-flight panel listed, which can live anywhere a
    session pointed `--log-dir` or `$CLAUDE_JOB_DIR`. Only a name in this index is ever read.
    """
    found: list[Path] = list(seen)
    for d in dirs:
        try:
            found.extend(d.iterdir())
        except OSError:
            continue
    return {p.name: p for p in found if landing_log(p, window_s, now)}


def read_log(path: Path) -> tuple[str, bool]:
    """The log's text, up to `MAX_LOG_BYTES`, and whether it was cut there."""
    with path.open("rb") as f:
        data = f.read(MAX_LOG_BYTES + 1)
    return data[:MAX_LOG_BYTES].decode("utf-8", errors="replace"), len(
        data
    ) > MAX_LOG_BYTES


def parse_landing(text: str) -> dict:
    """A landing log as its PR, stages and verdict.

    A stage starts at each `== arm` or `== <n>/<of>` header. A `== ` line without a number
    (a handoff, a relay note) is a detail of the stage it sits in. Lines before the first
    header form a stage with an empty step. Each detail is `[text, count]`: a wait prints the
    same line once a poll, and a run of identical lines folds into one with its count.
    """
    stages: list[dict] = []
    verdict, pr, of = None, "", 0
    for line in text.splitlines():
        if not pr and (m := _PR_IN_LOG.search(line)):
            pr = m.group(1)
        if v := _VERDICT_LINE.match(line):
            tone, hint = VERDICT_HINTS.get(v.group(1), ("warn", ""))
            verdict = {
                "verdict": v.group(1),
                "detail": v.group(2) or "",
                "tone": tone,
                "hint": hint,
            }
            continue
        if s := _STAGE.match(line):
            of = max(of, int(s.group(2) or 0))
            step = s.group(1) if s.group(1) is not None else "arm"
            stages.append({"step": step, "label": s.group(3), "lines": []})
            continue
        if not stages:
            stages.append({"step": "", "label": "", "lines": []})
        lines, text = stages[-1]["lines"], line.removeprefix("  ")
        if lines and lines[-1][0] == text:
            lines[-1][1] += 1
        else:
            lines.append([text, 1])
    return {"pr": pr, "of": of, "stages": stages, "verdict": verdict}
