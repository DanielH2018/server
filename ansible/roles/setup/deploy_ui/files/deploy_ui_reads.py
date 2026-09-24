"""Readers behind the four panels of the deploy UI. Pure parsers, stdlib only.

The daemon runs under `uv run --no-project` on the host interpreter, outside the repo venv,
so nothing here imports from `scripts/` or `/opt/gitops-deploy` — `gitops_markers`, the
deployer's own marker table, is a generated copy beside this file. `land.py`, `probe.py` and
`gh` are reached as subprocesses by `deploy_ui.App`; this module turns their text into rows.
"""

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

import gitops_markers

# The five markers the panels show, by basename — `gitops_markers` is the deployer's own table,
# copied into this `files/` (its header says how it is kept fresh). The basenames are also the
# keys `/api/state` serves, which is what the page reads.
MARKERS = tuple(
    gitops_markers.MARKERS[m]
    for m in ("hold", "hold_plane", "last_run", "behind", "staging_override")
)
_OVERRIDE = gitops_markers.MARKERS["staging_override"]
_HOLD_PLANE = gitops_markers.MARKERS["hold_plane"]

# Between the entries of a `hold_plane` that more than one failed apply wrote (#2381). A
# literal, because the daemon runs under `uv run --no-project` outside the repo venv and
# cannot import `deploy_git.HOLD_PLANE_SEP`, which pytest asserts this matches.
HOLD_PLANE_SEP = "; "

# `deploy_run.py`: the `deploy.sh` shim execs `uv run … deploy_run.py`, and that `uv` process
# stays the family root while the locked half runs under it (#2412), so no process says
# `deploy.sh` at all.
_RUN_RE = re.compile(r"\b(land\.py|deploy\.sh|deploy_run\.py|ansible-playbook)\b")
_PR_RE = re.compile(r"--pr\s+(\d+)")
_TAGS_RE = re.compile(r"--tags[= ]+(\S+)")
# `deploy.sh --list-services` is a read this daemon itself runs on every deploy POST, and
# `grep` is whoever is looking for a run. Neither is a run.
_NOT_A_RUN = re.compile(r"^grep\b|--list-services\b")


@dataclass(frozen=True)
class Proc:
    pid: int
    ppid: int
    elapsed_s: int
    args: str


@dataclass(frozen=True)
class Run:
    """One process family in flight: a landing, a deploy, or a bare lock holder.

    `locks` names the lock files the family holds and `waiting_on` the ones it is queued
    on, both by basename. `deploy.sh` opens a service lock's descriptor and then blocks
    on it, so a queued second deploy of the same service has the file OPEN for the whole
    wait and fuser lists it like the holder; only the kernel's lock table (`/proc/locks`)
    tells the two apart. A `deploy` row with a `waiting_on` is the queue that was
    silent before #1844.
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
# line can be dead: `deploy.sh` takes its locks with a `flock` CHILD on an inherited
# descriptor, and the lock outlives the child. The pid on a `->` line is alive by
# construction, blocked inside flock(2).
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
    below it. A landing is `uv run … land.py`, its python child, and the `deploy.sh` and
    `ansible-playbook` it spawns: one row, kind `land`. A page-spawned deploy is
    `deploy.sh`, `uv run ansible-playbook` and its child: one row, kind `deploy`. A lock
    holder that is neither — the GitOps tick on the tree lock — is a row of kind `lock`,
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
        if "land.py" in p.args:
            kind = "land"
        elif _is_run(p.args):
            kind = "deploy"
        else:
            kind = "lock"
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


def hold_plane_entries(held: str) -> list[str]:
    """Each failed apply a `hold_plane` marker records, oldest first.

    One entry per apply that failed since the hold was taken, not one hold. The page shows
    them separately because a Clear drops all of them at once (#2453).
    """
    return [e.strip() for e in held.split(";") if e.strip()]


def read_state(state_dir: Path) -> dict[str, str | list[str]]:
    """Read the five markers, plus `hold_plane` split into its entries.

    A missing marker is ''. The override is presence-only, so it reads 'set' or ''. The
    `hold_plane_entries` key is the parsed form of `hold_plane`, and it is what the page
    lists; the raw string stays beside it.

    Raises:
        OSError: a marker exists but can't be read (e.g. permission denied). Only a
            missing marker is a clear state; anything else that stops the read must not
            be mistaken for one.
    """
    st: dict[str, str | list[str]] = {}
    for name in MARKERS:
        p = state_dir / name
        try:
            text = p.read_text()
        except FileNotFoundError:
            st[name] = ""
            continue
        st[name] = "set" if name == _OVERRIDE else text.strip()
    st["hold_plane_entries"] = hold_plane_entries(str(st[_HOLD_PLANE]))
    return st


def parse_stale(text: str) -> list[dict]:
    """Parse `probe.py releases --stale-only` lines of the form `svc: reason`.

    Its no-stale summary has no colon-space before a service name, so it yields nothing.
    """
    rows = []
    for line in text.splitlines():
        svc, sep, reason = line.partition(": ")
        if sep and " " not in svc:
            rows.append({"service": svc, "reason": reason.strip()})
    return rows


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
