"""`probe.py gitops-state` — every marker the GitOps deployer keeps, read-only, with its way out.

`probe.py landing` reads `hold_sha` and the `manual_plane` class only, and the one other place
that printed the markers was the tail of `gitops_tick.sh`, which runs a real tick first (#3931).
This reads the deployer's state directory and prints, without ticking:

- `last_run`, whose age tells "ticked, nothing to do" from "did not tick".
- `hold_sha` with the planes its `hold_plane` lines wait on, and `gitops_markers.HOLD_CLEAR_CMD`.
- `behind_since`, `diverged_sha` and `contention_since`. Only the last has an operator clear;
  the tick rewrites the other two itself.
- Every other `owed` ledger class: `manual_plane`, `k8s_deferred`, `k8s_unapplied`, each line
  with the apply or deploy that discharges it and the `clear-owed` command to run after.

Every parser and every command string is the deployer's own, imported from its `files/`, so
this view cannot word a marker or a clear differently from the surfaces that page on it.

ABSENT IS NOT UNREADABLE. The directory is 0750 and owned by the deploy user, so another user
on daniel-box sees the files exist and cannot open them, and every other host has no directory
at all. `deployer_park`'s readers fold both into "absent", which here would print "no hold".
This reports each marker as set, absent or unreadable instead.

Exit code: 0 when every marker was read (absent counts as read), 1 when the directory or any
marker could not be. The exit says nothing about what the markers hold.
"""

# `probe_lib` is a namespace package under `scripts/`, so reaching a sibling by package name
# needs `scripts/` on sys.path — a module gets only its importer's path otherwise, and
# pyproject's `pythonpath` is a pytest setting. This has to sit ABOVE the imports below.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import json
import time
from pathlib import Path

from lib.deployer_park import _age_phrase
from lib.repo_paths import GITOPS_DEPLOY_FILES

# The deployer's own modules, so each format and each clear command is its own.
_sys.path.insert(0, str(GITOPS_DEPLOY_FILES))
from gitops_ledger import (
    OWED_K8S_DEFERRED,
    OWED_K8S_UNAPPLIED,
    OWED_MANUAL_PLANE,
    held_planes,
    manual_plane_entries,
    parse_owed,
)
from gitops_markers import (
    CONTENTION_CLEAR_CMD,
    HOLD_CLEAR_CMD,
    MARKERS,
    NO_PLAYBOOK,
    STATE_DIR,
    k8s_deferred_deploy_cmd,
    maximal_apply_warning,
    owed_clear_cmd,
    parse_behind,
    parse_contention,
    target_arg,
)

SET, ABSENT, UNREADABLE = "set", "absent", "unreadable"

# The owed classes printed one line per entry. The `hold_plane` class is printed under the
# hold, since its lines clear only together with `hold_sha`.
OWED_LISTED = (OWED_MANUAL_PLANE, OWED_K8S_DEFERRED, OWED_K8S_UNAPPLIED)


def read_marker(state_dir: Path, name: str) -> tuple[str, str | None]:
    """`(status, stripped text)` for one marker; the text is None unless the status is `set`.

    An empty file is `absent`: every writer removes a marker rather than emptying it, and
    every reader treats the two alike.
    """
    try:
        text = (state_dir / MARKERS[name]).read_text(errors="replace").strip()
    except FileNotFoundError:
        return ABSENT, None
    except OSError:
        return UNREADABLE, None
    return (SET, text) if text else (ABSENT, None)


def _owed_rows(owed: str | None, now: float) -> dict[str, list[dict]]:
    """Each listed class's ledger lines, oldest first, with what discharges each."""
    rows: dict[str, list[dict]] = {cls: [] for cls in OWED_LISTED}
    entries, narrow = manual_plane_entries(owed)
    for e in entries:
        selected = narrow.get(e.role) or frozenset({e.role})
        apply = (
            f"{e.playbook} --tags {','.join(sorted(selected))}{target_arg(e.role)}"
            if e.playbook != NO_PLAYBOOK
            else "apply the role by hand; no playbook applies it"
        )
        warning = maximal_apply_warning(e.role, selected)
        rows[OWED_MANUAL_PLANE].append(
            {
                "subject": e.role,
                "origin": e.origin,
                "age_s": int(now - e.at),
                "do": apply + (f" (WARNING: {warning})" if warning else ""),
                "clear": owed_clear_cmd(OWED_MANUAL_PLANE, e.role, selected),
            }
        )
    for cls in (OWED_K8S_DEFERRED, OWED_K8S_UNAPPLIED):
        for e in sorted(parse_owed(owed, cls), key=lambda e: e.at):
            rows[cls].append(
                {
                    "subject": e.subject,
                    "origin": e.origin,
                    "age_s": int(now - e.at),
                    "do": k8s_deferred_deploy_cmd([e.subject]),
                    "clear": owed_clear_cmd(cls, e.subject),
                }
            )
    return rows


def collect(state_dir: str | Path = STATE_DIR, now: float | None = None) -> dict:
    """Read every marker once into one snapshot. Writes nothing and takes no lock.

    Args:
        state_dir: the deployer's state directory.
        now: the clock ages are measured against; the wall clock by default.

    Returns:
        The snapshot `format_text` prints and `--json` dumps. `unreadable` names every marker
        that exists and could not be read; a field that depends on one is `null`.
    """
    state_dir = Path(state_dir)
    now = time.time() if now is None else now
    snap: dict = {
        "state_dir": str(state_dir),
        "read_at": int(now),
        "directory": SET if state_dir.is_dir() else ABSENT,
        "unreadable": [],
    }
    raw = {}
    for name in ("last_run", "hold", "behind", "diverged", "contention", "owed"):
        status, text = read_marker(state_dir, name)
        if snap["directory"] == ABSENT:
            status = ABSENT
        elif status == UNREADABLE:
            snap["unreadable"].append(MARKERS[name])
        raw[name] = (status, text)

    status, text = raw["last_run"]
    try:
        at = float(text) if text else None
    except ValueError:
        at = None
    snap["last_run"] = {
        "status": status,
        "at": at,
        "age_s": None if at is None else int(now - at),
    }

    owed_status, owed = raw["owed"]
    owed_known = owed_status != UNREADABLE
    status, sha = raw["hold"]
    planes = held_planes(owed) if owed_known else None
    # Planes with no `hold_sha` are what a hand `rm` of it leaves; the next hold waits on them.
    orphaned = status == ABSENT and bool(planes)
    snap["hold"] = {
        "status": status,
        "sha": sha,
        "planes": planes,
        "clear": f"{HOLD_CLEAR_CMD} {sha}"
        if sha
        else f"{HOLD_CLEAR_CMD} --orphaned"
        if orphaned
        else None,
    }

    status, text = raw["behind"]
    behind = parse_behind(text)
    snap["behind"] = {
        "status": status,
        "origin": behind[0] if behind else None,
        "age_s": int(now - behind[1]) if behind else None,
    }

    status, text = raw["diverged"]
    snap["diverged"] = {"status": status, "origin": text}

    status, text = raw["contention"]
    streak = parse_contention(text)
    snap["contention"] = {
        "status": status,
        "origin": streak.origin if streak else None,
        "lock": streak.lock if streak else None,
        "count": streak.count if streak else None,
        "age_s": int(now - streak.first_seen) if streak else None,
        "clear": CONTENTION_CLEAR_CMD if status == SET else None,
    }

    snap["owed_status"] = owed_status
    snap["owed"] = _owed_rows(owed, now) if owed_known else None
    return snap


def _span(age_s: int | None) -> str:
    return "an unknown time" if age_s is None else _age_phrase(age_s)


def _ago(age_s: int | None) -> str:
    return "age unknown" if age_s is None else f"{_age_phrase(age_s)} ago"


def format_text(snap: dict) -> str:
    """The snapshot as lines for a terminal: one line per marker, then the ledger."""
    lines = [f"GitOps deployer state ({snap['state_dir']}), read-only:"]
    if snap["directory"] == ABSENT:
        lines.append(
            "  no state directory here: the deployer runs on daniel-box, as the deploy user"
        )
        return "\n".join(lines)

    def unreadable(name: str) -> str:
        return (
            f"unreadable as this user ({name} is the deploy user's; an agent user reads it "
            "through the ACL that `initial_setup.yml --tags claude_code` or "
            "`--tags renovate_agent` grants, so check `getfacl /var/lib/gitops-deploy`)"
        )

    last = snap["last_run"]
    if last["status"] == UNREADABLE:
        lines.append(f"last_run:     {unreadable('last_run')}")
    elif last["at"] is None:
        lines.append(
            "last_run:     none — no tick has got far enough to write it"
            if last["status"] == ABSENT
            else "last_run:     garbled"
        )
    else:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(last["at"]))
        lines.append(f"last_run:     {stamp} ({last['age_s']}s ago)")

    hold = snap["hold"]
    if hold["status"] == UNREADABLE:
        lines.append(f"hold_sha:     {unreadable('hold_sha')}")
    elif hold["sha"]:
        planes = hold["planes"]
        waits = (
            "planes unknown, the owed ledger is unreadable"
            if planes is None
            else f"waiting on {len(planes)} plane(s)"
        )
        lines.append(f"hold_sha:     {hold['sha']}  <-- held, {waits}")
        lines += [f"  plane: {p}" for p in planes or []]
        lines.append(
            "  apply each plane, then (dropping hold_sha and every plane line): "
            f"{hold['clear']}"
        )
    elif hold["planes"]:
        lines.append(
            f"hold_sha:     none, but {len(hold['planes'])} orphaned hold_plane line(s)  "
            "<-- the next hold would wait on these too"
        )
        lines += [f"  plane: {p}" for p in hold["planes"]]
        lines.append(f"  apply any still owed, then: {hold['clear']}")
    else:
        lines.append("hold_sha:     none")

    behind = snap["behind"]
    if behind["status"] == UNREADABLE:
        lines.append(f"behind_since: {unreadable('behind_since')}")
    elif behind["status"] == SET:
        lines.append(
            f"behind_since: {behind['origin'] or 'garbled'}, not fast-forwarded for "
            f"{_span(behind['age_s'])}  <-- the tick clears it once it "
            "converges; `journalctl -t gitops-deploy` says why it has not"
        )
    else:
        lines.append("behind_since: none (converged with origin)")

    diverged = snap["diverged"]
    if diverged["status"] == UNREADABLE:
        lines.append(f"diverged_sha: {unreadable('diverged_sha')}")
    elif diverged["status"] == SET:
        lines.append(
            f"diverged_sha: {diverged['origin']}  <-- local and origin have diverged; "
            "reconcile the primary checkout by hand, and the tick clears it"
        )
    else:
        lines.append("diverged_sha: none")

    cont = snap["contention"]
    if cont["status"] == UNREADABLE:
        lines.append(f"contention:   {unreadable('contention_since')}")
    elif cont["status"] == SET:
        what = (
            f"{cont['count']} tick(s) deferred on service lock `{cont['lock']}` for "
            f"{_span(cont['age_s'])}"
            if cont["lock"]
            else "garbled"
        )
        lines.append(
            f"contention:   {what}  <-- end the lock's holder, then {cont['clear']}"
        )
    else:
        lines.append("contention:   none")

    if snap["owed"] is None:
        lines.append(f"owed ledger:  {unreadable('owed.jsonl')}")
    else:
        for cls in OWED_LISTED:
            rows = snap["owed"][cls]
            lines.append(f"{cls + ':':<15}{len(rows) or 'none'}")
            for r in rows:
                lines.append(
                    f"  {r['subject']}  (origin {r['origin'][:8]}, {_ago(r['age_s'])})"
                )
                lines.append(f"    do:   {r['do']}")
                lines.append(f"    then: {r['clear']}")
    return "\n".join(lines)


def run_gitops_state(
    ns, state_dir: str | Path = STATE_DIR, now: float | None = None
) -> int:
    """Print the snapshot, as text or with `--json` as one document; exit 1 on an unread marker.

    `state_dir` and `now` exist for a test; `probe.py` passes neither.
    """
    snap = collect(state_dir, now)
    print(json.dumps(snap) if ns.json else format_text(snap))
    return 1 if snap["directory"] == ABSENT or snap["unreadable"] else 0
