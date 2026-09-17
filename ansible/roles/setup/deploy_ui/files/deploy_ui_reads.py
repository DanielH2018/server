"""Readers behind the four panels of the deploy UI. Pure parsers, stdlib only.

The daemon runs under `uv run --no-project` on the host interpreter, outside the repo venv,
so nothing here imports from `scripts/` or `/opt/gitops-deploy`. `land.py`, `probe.py` and
`gh` are reached as subprocesses by `deploy_ui.App`; this module turns their text into rows.
"""

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

MARKERS = (
    "hold_sha",
    "hold_plane",
    "last_run",
    "behind_since",
    "staging_gate_override",
)
_RUN_RE = re.compile(r"\b(land\.py|deploy\.sh|ansible-playbook)\b")
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

    `locks` names the lock files the family holds, by basename. A `deploy` row with no
    locks is queued on one: `deploy.sh` takes its service locks before the playbook, so a
    second deploy of the same service waits there, silently, for the first.
    """

    pid: int
    elapsed_s: int
    kind: str
    pr: str
    tag: str
    args: str
    locks: tuple[str, ...]
    log: str = ""


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


def runs(procs: dict[int, Proc], held: dict[str, set[int]]) -> list[Run]:
    """Fold processes into one row per family.

    The topmost run or lock holder on a pid's ancestor chain owns every run and holder
    below it. A landing is `uv run … land.py`, its python child, and the `deploy.sh` and
    `ansible-playbook` it spawns: one row, kind `land`. A page-spawned deploy is
    `deploy.sh`, `uv run ansible-playbook` and its child: one row, kind `deploy`. A lock
    holder that is neither — the GitOps tick on the tree lock — is a row of kind `lock`,
    so a held lock is never invisible because its holder matched no pattern. fuser lists
    every process with the file open, children included, which is why the fold has to
    include holders and not only runs.
    """
    holders = set().union(*held.values()) if held else set()
    candidates = {pid for pid, p in procs.items() if _is_run(p.args)} | (
        holders & procs.keys()
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
        locks = tuple(
            sorted(
                os.path.basename(path) for path, pids in held.items() if pids & members
            )
        )
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
                locks,
            )
        )
    return sorted(rows, key=lambda r: r.pid)


def log_path_of(pid: int) -> str:
    """Where the process's stdout goes, or '' when unreadable."""
    try:
        return os.readlink(f"/proc/{pid}/fd/1")
    except OSError:
        return ""


def read_state(state_dir: Path) -> dict[str, str]:
    """Read the five markers as strings.

    A missing marker is ''. The override is presence-only, so it reads 'set' or ''.

    Raises:
        OSError: a marker exists but can't be read (e.g. permission denied). Only a
            missing marker is a clear state; anything else that stops the read must not
            be mistaken for one.
    """
    st = {}
    for name in MARKERS:
        p = state_dir / name
        try:
            text = p.read_text()
        except FileNotFoundError:
            st[name] = ""
            continue
        st[name] = "set" if name == "staging_gate_override" else text.strip()
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
