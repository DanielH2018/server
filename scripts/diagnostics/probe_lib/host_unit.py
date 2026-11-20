"""`probe.py unit` — a host unit's run state: its result, last run, next trigger and journal.

Sessions read this by hand, 99 times in two weeks, with `systemctl show`, `systemctl list-timers`
and `journalctl -u`/`-t`, often over ssh. This makes one `host_reader.read_host` call on
the named host and prints:

- the service's ActiveState, SubState and Result, and its last start and end;
- the timer's last and next trigger, when the unit has a timer;
- the newest journal lines that are not systemd's routine start and stop lines, read from the
  unit and from its syslog identifiers. A unit's scripts often log through `logger -t`, under
  the stem (`aide-check`) or the full name (`homelab-builder`), so both are read. A line in
  both reads is printed once.

systemd resets `Result` to `success` and clears the run timestamps when it reloads a unit, so an
empty `ExecMainStartTimestamp` is no record of the last run. The result then comes from
systemd's own line in the journal (`Failed with result`, `Deactivated successfully`). When
neither holds it but the timer or the journal shows a run, the result reads unknown and the
probe exits 75.

Exit 1 when the service is not loaded, its last run did not end `success`, or its timer is
loaded but has no next trigger while the service is idle. A monotonic timer schedules its
next trigger only once a run ends, so a running service explains a missing one. 75 when the host could not be read; a journal this user may
not read prints the unit state and exits 75 too. `unitq` stays the path for a unit's file.
"""

import re
import time

from diagnostics.probe_lib import core
from diagnostics.probe_lib.host_reader import (
    BOX,
    dry_run_text,
    journal_entries,
    read_host,
)
from lib.exit_codes import PROBE_FAILED, PROBE_OK, PROBE_UNREADABLE
from lib.inventory_lists import host_names

SHOW = [
    "LoadState",
    "ActiveState",
    "SubState",
    "Result",
    "ExecMainStartTimestamp",
    "ExecMainExitTimestamp",
    "ExecMainStatus",
]
TIMER_SHOW = ["LoadState", "ActiveState"]
RUNNING = frozenset({"activating", "deactivating", "reloading"})
# Lines read per source before the routine ones are dropped.
READ_LINES = 200
# systemd writes a few lines per run, so this reaches back several runs.
OUTCOME_LINES = 20
# A leading `-` would reach systemctl and journalctl as an option.
_UNIT_NAME = re.compile(r"[A-Za-z0-9_@][A-Za-z0-9_@.:\\-]*")
_ROUTINE = re.compile(
    r"^(Starting|Started|Finished|Stopping|Stopped) |Deactivated successfully|Consumed .* CPU time"
)

_FAILED = re.compile(r"Failed with result '([^']+)'")
_SUCCEEDED = re.compile(r"Deactivated successfully|^Finished |: Succeeded\.$")

UNIT_HELP = (
    "a host unit's run state: ActiveState and Result, the last start and end, the timer's "
    "next trigger, and the newest non-routine journal lines from the unit and its syslog tag",
    "the unit is loaded, its last run succeeded, and its timer has a next trigger",
    "the unit is not loaded, its last run failed, or its timer has no next trigger",
)


def base_name(unit: str) -> str:
    """`unit` without a `.service` or `.timer` suffix.

    Raises:
        core.UsageError: `unit` is not a unit name.
    """
    if not _UNIT_NAME.fullmatch(unit):
        raise core.UsageError(f"unit: {unit!r} is not a unit name")
    return re.sub(r"\.(service|timer)$", "", unit)


def tags(base: str) -> list[str]:
    """The syslog identifiers a unit's scripts log under: its name, and its name without `homelab-`."""
    stem = base.removeprefix("homelab-")
    return sorted({base, stem})


def plan(base: str, tag_names: list[str]) -> dict:
    journal = {"unit": [f"{base}.service", None, None, "-u", READ_LINES]}
    journal |= {f"tag:{t}": [t, None, None, "-t", READ_LINES] for t in tag_names}
    # A run that logs more than READ_LINES pushes systemd's outcome line out of the unit read.
    journal["systemd"] = [f"UNIT={base}.service", None, None, "match", OUTCOME_LINES]
    return {
        "show": {f"{base}.service": SHOW, f"{base}.timer": TIMER_SHOW},
        "timers": [f"{base}.timer"],
        "journal": journal,
    }


def _stamp(at: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(at))


def _or_never(value: str | None) -> str:
    return value or "never"


def journal_lines(doc: dict) -> tuple[list, list[str]]:
    """(the entries of every journal read, newest last and each once; the reads that failed)."""
    seen, entries, unread = set(), [], []
    for key in doc.get("journal") or {}:
        try:
            read = journal_entries(doc, key)
        except core.Unreadable as exc:
            unread.append(exc.message)
            continue
        for e in read:
            if (e.at, e.message) not in seen:
                seen.add((e.at, e.message))
                entries.append(e)
    return sorted(entries, key=lambda e: e.at), unread


def is_routine(entry) -> bool:
    """systemd's own start, stop and accounting lines, which every run repeats."""
    return entry.identifier == "systemd" and bool(_ROUTINE.search(entry.message))


def journal_result(entries) -> str | None:
    """How the newest run in the journal ended, from systemd's own line; None when none says."""
    for e in reversed(entries):
        if e.identifier != "systemd":
            continue
        failed = _FAILED.search(e.message)
        if failed:
            return failed.group(1)
        if _SUCCEEDED.search(e.message):
            return "success"
    return None


def report(doc: dict, base: str, *, now: float, last: int) -> tuple[str, int]:
    """The text view and the exit code for one read of the unit's host."""
    show = doc.get("show") or {}
    service = show.get(f"{base}.service") or {}
    timer = show.get(f"{base}.timer") or {}
    host = doc.get("host")
    if service.get("LoadState") != "loaded":
        return (
            f"unit: {base}.service is {service.get('LoadState') or 'unreadable'} on {host}",
            PROBE_FAILED,
        )
    problems = []
    entries, unread = journal_lines(doc)
    prev = ((doc.get("timers") or {}).get(f"{base}.timer") or {}).get("last") or 0
    result = service.get("Result") or "?"
    status = service.get("ExecMainStatus") or "?"
    unknown = False
    if not service.get("ExecMainStartTimestamp"):
        # systemd resets Result to success and clears the timestamps when it reloads a unit.
        result, status = journal_result(entries), "?"
        if result is None:
            unknown = bool(prev or entries)
            result = "unknown (systemd holds no run)" if unknown else "none (never ran)"
        else:
            result += " (from the journal)"
    if not unknown and not result.startswith(("success", "none")):
        problems.append(f"the last run ended {result}")
    lines = [
        f"{base}.service ({host}): {service.get('ActiveState')}/{service.get('SubState')}, "
        f"result {result}, exit status {status}",
        f"  last start {_or_never(service.get('ExecMainStartTimestamp'))}",
        f"  last end   {_or_never(service.get('ExecMainExitTimestamp'))}",
    ]
    if timer.get("LoadState") == "loaded":
        row = (doc.get("timers") or {}).get(f"{base}.timer") or {}
        nxt = row.get("next") or 0
        # A monotonic timer schedules its next trigger only once its service run ends.
        running = (
            service.get("ActiveState") in RUNNING
            or service.get("SubState") == "running"
        )
        if not nxt and not running:
            problems.append(
                f"{base}.timer ({timer.get('ActiveState')}) has no next trigger"
            )
        if nxt:
            upcoming = f"{_stamp(nxt / 1e6)} (in {core.age_phrase(max(0, int(nxt / 1e6 - now)))})"
        else:
            upcoming = "after the running service ends" if running else "none"
        lines.append(
            f"{base}.timer: {timer.get('ActiveState')}, last trigger "
            f"{_stamp(prev / 1e6) if prev else 'never'}, next {upcoming}"
        )
    else:
        lines.append(f"{base}.timer: none")
    shown = [e for e in entries if not is_routine(e)][-last:]
    lines.append(f"journal, newest {len(shown)} non-routine lines:")
    lines += [f"  {_stamp(e.at)}  {e.identifier}: {e.message}" for e in shown] or [
        "  none"
    ]
    lines += [f"  unread: {u}" for u in unread]
    if unknown:
        unread.append(
            "the last run's result is in neither systemd nor the journal read"
        )
    lines.append(
        f"verdict: {'; '.join(problems) or ('result unknown' if unknown else 'ok')}"
    )
    code = PROBE_FAILED if problems else (PROBE_UNREADABLE if unread else PROBE_OK)
    return "\n".join(lines), code


def run_unit(ns, read=read_host, now: float | None = None) -> int:
    """Read the unit's host once, print the report, return its exit code."""
    base = base_name(ns.unit)
    request = plan(base, tags(base))
    if ns.dry_run:
        print(dry_run_text(request, ns.host))
        return PROBE_OK
    text, code = report(
        read(request, ns.host),
        base,
        now=time.time() if now is None else now,
        last=ns.last,
    )
    print(text)
    return code


def add_parser(sub, verdict_help) -> None:
    """Add `unit` to probe.py's subparsers; it lives here while cli_parser.py sits at its cap."""
    up = sub.add_parser("unit", **verdict_help(*UNIT_HELP))
    up.add_argument(
        "unit", help="the unit, e.g. homelab-aide-check or homelab-builder.timer"
    )
    up.add_argument(
        "--host",
        choices=host_names(),
        default=BOX,
        help="the host it runs on (default daniel-box)",
    )
    up.add_argument(
        "-n", dest="last", type=int, default=10, help="journal lines to show"
    )
    up.add_argument(
        "--dry-run", action="store_true", help="print the read without making it"
    )
