"""`probe.py monitors --checks [NAME]` — each monitor-bridge check's live verdict and why.

`monitors` lists what fires. A check can be red without firing: held `up` by its `*_CONSECUTIVE`
streak, skipped behind a down gate, or missing its rule in `bridge-exporter.yaml`. Sessions read
those cases by hand through `probe.py metric` (#8860). This prints one line per exported check:
its value, the age of its last run, and its notes:

- `down` and the members the row names, from `homelab_check_success` and `homelab_check_detail`;
- `skipped: gate <g> down`, when the row's gate in `check_table.CHECKS` is itself an exported
  row reading 0. A gate with no row of its own (`startup_grace`) reads as up, as the bridge
  treats it;
- `stale`, when the last run is older than the row's own `homelab_check_deadline_seconds`;
- `down streak n/N`, from the bridge's own log line in loki-homelab. A held cycle exports 1, so
  Prometheus cannot show it. The line counts only when it was logged in the cycle that recorded
  the row's last run, because a streak resets without a line;
- `no rule`, when `bridge-exporter.yaml` names no `check="<row>"`: that row never pages.

A table row with no series reads `not exported` (disabled by `CHECKS_ONLY` or `CHECKS_SKIP`).
Exit 1 when a row is down or stale (a skipped row is its gate's page), 75 when Prometheus could
not be read or holds no check series. An unread Loki prints `streak notes unread` and leaves
the code alone, because a held row exports 1 either way.
"""

# `probe_lib` is a namespace package under `scripts/`, so reaching a sibling by package name
# needs `scripts/` on sys.path; pyproject's `pythonpath` is a pytest setting. This has to sit
# ABOVE the imports below.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import re
import time
from dataclasses import dataclass, field

from diagnostics.probe_lib import core
from diagnostics.probe_lib.core import _rows_from_loki, loki_endpoint, prom_query_url
from diagnostics.probe_lib.obs_api import loki_query_url
from lib.exit_codes import PROBE_FAILED, PROBE_OK
from lib.repo_paths import REPO
from renderer.template_filters.py_table import py_table

CHECK_QUERY = (
    '{__name__=~"homelab_check_(success|last_run_timestamp_seconds|deadline_seconds|detail)",'
    'job="monitor-bridge"}'
)
STREAK_LOGQL = '{container="monitor-bridge"} |= "down streak"'
# Two 300s cycles: the newest held cycle always falls inside it.
STREAK_WINDOW_S = 600
# How far a streak line's ingestion time may sit from the row's last-run stamp and still be
# that cycle's line.
SAME_CYCLE_S = 120
CHECK_TABLE = REPO / "k8s" / "roles" / "monitor-bridge" / "files" / "check_table.py"
RULES = (
    REPO
    / "k8s"
    / "roles"
    / "observability"
    / "files"
    / "rules"
    / "bridge-exporter.yaml"
)

_STREAK = re.compile(r"^OK\s+(\S+) - (down streak \d+/\d+)")
_RULED = re.compile(r'check="([^"]+)"')


@dataclass
class Row:
    """One check's exported series."""

    value: float | None = None
    last_run: float | None = None
    deadline: float | None = None
    details: list[str] = field(default_factory=list)


def rows_of(result: list[dict]) -> dict[str, Row]:
    """Each check's series from the response to `CHECK_QUERY`."""
    rows: dict[str, Row] = {}
    for s in result:
        labels = s.get("metric") or {}
        name, metric = labels.get("check"), labels.get("__name__", "")
        if not name:
            continue
        row = rows.setdefault(name, Row())
        value = float(s["value"][1])
        if metric == "homelab_check_success":
            row.value = value
        elif metric == "homelab_check_last_run_timestamp_seconds":
            row.last_run = value
        elif metric == "homelab_check_deadline_seconds":
            row.deadline = value
        elif metric == "homelab_check_detail":
            row.details.append(labels.get("detail", "?"))
    return rows


def gates(source: str) -> dict[str, str]:
    """Each table row's gate, from `check_table.py`'s source."""
    return {r["name"]: r["gate"] for r in py_table(source, "CHECKS") if r.get("gate")}


def ruled(rules_text: str) -> set[str]:
    """The checks `bridge-exporter.yaml` has a per-check rule for."""
    return set(_RULED.findall(rules_text))


def streaks(lines: list[tuple[int, str]], rows: dict[str, Row]) -> dict[str, str]:
    """Each check's `down streak n/N` note, kept only when logged in the row's newest cycle."""
    notes = {}
    for ns, line in lines:
        m = _STREAK.match(line)
        if not m or m.group(1) not in rows:
            continue
        last_run = rows[m.group(1)].last_run
        if last_run is not None and abs(ns / 1e9 - last_run) <= SAME_CYCLE_S:
            notes[m.group(1)] = m.group(2)
    return notes


def report(
    rows: dict[str, Row],
    *,
    table_gates: dict[str, str],
    table_names: list[str],
    with_rule: set[str],
    held: dict[str, str] | str,
    now: float,
    only: str = "",
) -> tuple[str, int]:
    """The text view and the exit code. `held` is the streak notes, or why Loki went unread."""
    names = [n for n in sorted(set(rows) | set(table_names)) if only in n]
    if only and not names:
        raise core.UsageError(f"monitors --checks: no check matches {only!r}")
    counts = {"up": 0, "down": 0, "skipped": 0, "stale": 0, "not exported": 0}
    lines = []
    for name in names:
        row = rows.get(name)
        if row is None or row.value is None:
            counts["not exported"] += 1
            lines.append(f"  {name:<28}-  not exported")
            continue
        notes = []
        gate = table_gates.get(name)
        if row.value == 0:
            gate_row = rows.get(gate) if gate else None
            if gate_row is not None and gate_row.value == 0:
                state = "skipped"
                notes.append(f"skipped: gate {gate} down")
            else:
                state = "down"
                notes.append(
                    "down"
                    + (f": {', '.join(sorted(row.details))}" if row.details else "")
                )
        else:
            state = "up"
            if isinstance(held, dict) and name in held:
                notes.append(held[name])
        age = None if row.last_run is None else int(now - row.last_run)
        if age is None or (row.deadline is not None and age > row.deadline):
            notes.append(f"stale: past {int(row.deadline or 0)}s")
            if state == "up":
                state = "stale"
        if name not in with_rule:
            notes.append("no rule in bridge-exporter.yaml, never pages")
        counts[state] += 1
        when = "never" if age is None else f"{core.age_phrase(age)} ago"
        lines.append(
            f"  {name:<28}{int(row.value)}  {when:<10}{'; '.join(notes)}".rstrip()
        )
    head = ", ".join(f"{n} {k}" for k, n in counts.items() if n) or "0 checks"
    out = [f"{len(names)} checks: {head}"]
    if isinstance(held, str):
        out.append(f"  streak notes unread: {held}")
    out.append(f"  {'check':<28}v  {'last run':<10}note")
    bad = counts["down"] or counts["stale"]
    return "\n".join(out + lines), PROBE_FAILED if bad else PROBE_OK


def _held(rows: dict[str, Row], now: float, fetch) -> dict[str, str] | str:
    base, pin = loki_endpoint()
    url = loki_query_url(
        base,
        STREAK_LOGQL,
        500,
        start=int((now - STREAK_WINDOW_S) * 1e9),
        end=int(now * 1e9),
    )
    try:
        return streaks(_rows_from_loki(fetch(url, resolve=pin)), rows)
    except core.Unreadable as exc:
        return exc.message


def print_checks(ns, now: float | None = None, fetch=core.fetch_parsed) -> int:
    """Read the check series and the streak lines, print the table, return the exit code."""
    now = time.time() if now is None else now
    base, pin = core.prom_endpoint()
    url = prom_query_url(base, CHECK_QUERY)
    if ns.dry_run:
        return core.print_dry_run(url, resolve=pin)
    data = fetch(url, resolve=pin)
    if data.get("status") != "success":
        raise core.Unreadable(
            f"monitors --checks: Prometheus answered {data.get('error') or data.get('status')!r}"
        )
    rows = rows_of(data["data"]["result"])
    if not rows:
        raise core.Unreadable(
            f"monitors --checks: Prometheus holds no series for {CHECK_QUERY}"
        )
    source = CHECK_TABLE.read_text()
    text, code = report(
        rows,
        table_gates=gates(source),
        table_names=[r["name"] for r in py_table(source, "CHECKS")],
        with_rule=ruled(RULES.read_text()),
        held=_held(rows, now, fetch),
        now=now,
        only=ns.checks,
    )
    print(text)
    return code
