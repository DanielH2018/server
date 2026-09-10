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
_LAND_RE = re.compile(r"\bland\.py\b")
_PR_RE = re.compile(r"--pr\s+(\d+)")


@dataclass(frozen=True)
class Landing:
    pid: int
    elapsed_s: int
    pr: str
    args: str
    log: str = ""


def parse_ps(text: str) -> list[Landing]:
    """`ps -eo pid=,etimes=,args=` lines to the land.py processes among them."""
    out = []
    for line in text.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3 or not parts[0].isdigit():
            continue
        pid, etimes, args = int(parts[0]), int(parts[1]), parts[2]
        if not _LAND_RE.search(args) or args.startswith("grep"):
            continue
        m = _PR_RE.search(args)
        out.append(Landing(pid, etimes, m.group(1) if m else "", args))
    return out


def log_path_of(pid: int) -> str:
    """Where the process's stdout goes, or '' when unreadable."""
    try:
        return os.readlink(f"/proc/{pid}/fd/1")
    except OSError:
        return ""


def parse_fuser_pid(stdout: str) -> int | None:
    """The flock parent: the lowest pid fuser lists on the lock file."""
    pids = [int(t) for t in stdout.split() if t.isdigit()]
    return min(pids) if pids else None


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
