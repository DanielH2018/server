"""`probe.py gitops-state --why` — the failure lines of the GitOps tick that wrote `hold_sha`.

`gitops-state` names the held commit and the planes it waits on, but not the error that put it
there, so 21 sessions in two weeks read the deployer's journal by hand for it (#6324).

The tick that held is found by time. Every writer of `hold_sha` writes the file whole
(`deploy_state.DeployerState.write_hold`, `hold_failed_apply`), so its mtime is the moment the
newest hold was recorded. This reads `gitops-deploy.service`'s journal from one run ceiling
(`TimeoutStartSec` in the unit template) before that moment to just after it, takes the unit
run whose last line is at or before the moment, and prints that run's failure lines: the
deployer's own lines at priority err or worse, and Ansible's `fatal:`, `FAILED!` and `ERROR!`
lines.

This adds to the view and judges nothing, so `gitops-state` still never exits 1 (#4468). A
journal it could not read exits 75; a run it could not find, because the journal has rotated
past it, is printed as such.
"""

# `probe_lib` is a namespace package under `scripts/`, so reaching a sibling by package name
# needs `scripts/` on sys.path — a module gets only its importer's path otherwise, and
# pyproject's `pythonpath` is a pytest setting. This has to sit ABOVE the imports below.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import re
import time
from pathlib import Path

from diagnostics.probe_lib import core
from diagnostics.probe_lib.host_reader import (
    BOX,
    journal_entries,
    read_host,
    unit_span_s,
)
from lib.repo_paths import GITOPS_DEPLOY_FILES

_sys.path.insert(0, str(GITOPS_DEPLOY_FILES))
from gitops_markers import MARKERS

UNIT = "gitops-deploy.service"
UNIT_TEMPLATE = GITOPS_DEPLOY_FILES.parent / "templates" / f"{UNIT}.j2"
# A hold is written as the tick fails, so its run's last lines land a moment after the mtime.
AFTER_S = 120
SHOWN = 15
CLIP = 400

# The deployer logs `<area>: <text>` (`k8s-deploy: failed for ...`, `broad: apply failed ...`),
# and each line of the playbook output its exception carries at err as well, so the `TASK [`
# header above a `fatal:` arrives at err too.
_DEPLOYER_LINE = re.compile(r"^(?:[a-z0-9_-]+: |TASK \[)")
_ANSIBLE_FAILURE = re.compile(r"^fatal: |FAILED! =>|^ERROR! ")

GITOPS_STATE_HELP = (
    "every GitOps deployer marker and owed-ledger line with its clear command, read-only",
    "every marker was read",
    "never: the view reports the markers and judges none; an unreadable marker or an "
    "absent state directory is 75",
)


def tick_ceiling_s(template: Path = UNIT_TEMPLATE) -> int:
    """The longest one tick may run, the unit's `TimeoutStartSec`."""
    return unit_span_s(template.read_text(), "TimeoutStartSec")


def is_failure_line(entry) -> bool:
    if entry.identifier == "sudo":
        return False
    if _ANSIBLE_FAILURE.search(entry.message):
        return True
    return entry.priority <= 3 and bool(_DEPLOYER_LINE.match(entry.message))


def held_tick(entries, held_at: float) -> list:
    """The entries of the unit run that logged the line nearest `held_at`.

    Only lines up to `AFTER_S` past `held_at` count, so the next tick, ten minutes on, is
    never taken for the one that held.
    """
    before = [e for e in entries if e.invocation and e.at <= held_at + AFTER_S]
    if not before:
        return []
    run = min(before, key=lambda e: abs(e.at - held_at)).invocation
    return [e for e in entries if e.invocation == run]


def explain(snap: dict, *, read=read_host, ceiling_s: int | None = None) -> dict | None:
    """The `why` section for `snap`, or None when no `hold_sha` is set.

    Raises:
        core.Unreadable: the hold file's mtime or the journal could not be read.
    """
    if not snap.get("hold", {}).get("sha"):
        return None
    hold = Path(snap["state_dir"]) / MARKERS["hold"]
    try:
        held_at = hold.stat().st_mtime
    except OSError as exc:
        raise core.Unreadable(f"gitops-state --why: cannot stat {hold}: {exc}") from exc
    ceiling_s = tick_ceiling_s() if ceiling_s is None else ceiling_s
    request = {
        "journal": {
            "deployer": [
                UNIT,
                f"@{int(held_at - ceiling_s)}",
                f"@{int(held_at + AFTER_S)}",
            ]
        }
    }
    doc = read(request, BOX)
    tick = held_tick(journal_entries(doc, "deployer"), held_at)
    failures = [e for e in tick if is_failure_line(e)]
    return {
        "held_at": int(held_at),
        "invocation": tick[0].invocation if tick else None,
        "started_at": int(tick[0].at) if tick else None,
        "lines": [
            {"at": int(e.at), "priority": e.priority, "message": e.message[:CLIP]}
            for e in failures[-SHOWN:]
        ],
        "omitted": max(0, len(failures) - SHOWN),
    }


def _stamp(at: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(at))


def format_why(why: dict | None) -> str:
    if why is None:
        return "why:          no hold_sha, so no tick to explain"
    if "unreadable" in why:
        return f"why:          unreadable: {why['unreadable']}"
    lines = [f"why:          hold_sha written {_stamp(why['held_at'])}"]
    if why["invocation"] is None:
        lines.append(
            f"  no {UNIT} run in the journal covers that moment (rotated past it?)"
        )
        return "\n".join(lines)
    lines.append(
        f"  by the tick whose first line in the window is at {_stamp(why['started_at'])} "
        f"(`journalctl _SYSTEMD_INVOCATION_ID={why['invocation']}` has all of it)"
    )
    if why["omitted"]:
        lines.append(f"  {why['omitted']} earlier failure line(s) omitted")
    lines += [f"  {_stamp(row['at'])}  {row['message']}" for row in why["lines"]] or [
        "  it logged no failure line; read the whole run"
    ]
    return "\n".join(lines)


def add_parser(sub, verdict_help) -> None:
    """Add `gitops-state` to probe.py's subparsers; it lives here while cli_parser.py sits at its cap."""
    gs = sub.add_parser("gitops-state", **verdict_help(*GITOPS_STATE_HELP))
    gs.add_argument(
        "--json", action="store_true", help="one JSON document instead of the text view"
    )
    gs.add_argument(
        "--why",
        action="store_true",
        help="also print the failure lines of the tick that wrote hold_sha (reads the "
        "deployer's journal; a `why` key under --json)",
    )
