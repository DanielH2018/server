"""`probe.py monitors --checks [NAME]` — each monitor-bridge check's live verdict and why.

`monitors` lists what fires. A check can be red without firing: held `up` by its `*_CONSECUTIVE`
streak, skipped behind a down gate, or missing its rule in `bridge-exporter.yaml`. Sessions read
those cases by hand through `probe.py metric`. This prints one line per exported check:
its value, the age of its last run, and its notes:

- `down` and the members the row names, from `homelab_check_success` and `homelab_check_detail`,
  then `rule <Alert>` for each `bridge-exporter.yaml` rule that selects the row;
- `skipped: gate <g> down`, when the row's gate in `check_table.CHECKS` is itself an exported
  row reading 0. A gate with no row of its own (`startup_grace`) reads as up, as the bridge
  treats it;
- `skipped: exporter <job> down`, when the row sits under that job in `check.py`'s
  `EXPORTER_DEPENDENT` and `up{job=...}` reads 0. The bridge skips such a row's body and
  exports 0, and ScrapeTargetDown pages for the exporter;
- `stale`, when the last run is older than the row's own `homelab_check_deadline_seconds`;
- `down streak n/N`, from the bridge's own log line in loki-homelab. A held cycle exports 1, so
  Prometheus cannot show it. The line counts only when it was logged in the cycle that recorded
  the row's last run, because a streak resets without a line;
- `no rule`, when `bridge-exporter.yaml` names no `check="<row>"`: that row never pages;
- `not in this checkout`, when Prometheus exports a row that this checkout's `check_table.py`
  lacks. The rules file is read from the same checkout, which predates the row, so its rule
  is unknown here rather than absent.

A table row with no series reads `not exported` (disabled by `CHECKS_ONLY` or `CHECKS_SKIP`).
Exit 1 when a row is down or stale (a skipped row is its gate's or its exporter's page), 75 when
Prometheus could not be read or holds no check series. An unread Loki prints `streak notes
unread` and leaves the code alone, because a held row exports 1 either way. An unread `up`
prints `exporter state unread`, and a row it would have skipped reads `down`, failing toward
alerting as the bridge does.
"""

import ast
import re
import time
from dataclasses import dataclass, field

from diagnostics.probe_lib import core
from diagnostics.probe_lib.core import _rows_from_loki, loki_endpoint, prom_query_url
from diagnostics.probe_lib.obs_api import loki_query_url
from lib import yaml_fast
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
SAME_CYCLE_S = 120
CHECK_TABLE = REPO / "k8s" / "roles" / "monitor-bridge" / "files" / "check_table.py"
CHECK_MODULE = CHECK_TABLE.with_name("check.py")
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


def exporter_dependent(source: str) -> dict[str, set[str]]:
    """`check.py`'s `EXPORTER_DEPENDENT`, job to the checks its exporter's death skips.

    Parsed rather than imported, for the reason `py_table` parses the check table: importing
    `check.py` imports every check body.

    Raises:
        ValueError: the source holds no `EXPORTER_DEPENDENT` dict of string sets.
    """
    for node in ast.walk(ast.parse(source)):
        target = (
            getattr(node, "target", None)
            or (getattr(node, "targets", None) or [None])[0]
        )
        if isinstance(target, ast.Name) and target.id == "EXPORTER_DEPENDENT":
            table = next(n for n in ast.walk(node) if isinstance(n, ast.Dict))
            return {
                ast.literal_eval(k): set(ast.literal_eval(v.args[0]))
                for k, v in zip(table.keys, table.values, strict=True)
                if k is not None and isinstance(v, ast.Call)
            }
    raise ValueError("check.py holds no EXPORTER_DEPENDENT")


def suppressed_checks(up: list[dict], dependent: dict[str, set[str]]) -> dict[str, str]:
    """Each check a down exporter skips, mapped to its job, as `check._exporter_suppressed` does."""
    down = {(s.get("metric") or {}).get("job") for s in up if float(s["value"][1]) == 0}
    return {
        name: job for job, names in dependent.items() if job in down for name in names
    }


def rules_by_check(rules_text: str) -> dict[str, list[str]]:
    """Each check `bridge-exporter.yaml` selects, mapped to the alerts whose `expr` selects it."""
    alerts: dict[str, list[str]] = {}
    for group in yaml_fast.parse_yaml(rules_text)["groups"]:
        for rule in group["rules"]:
            if "alert" not in rule:
                continue
            for check in _RULED.findall(rule["expr"]):
                alerts.setdefault(check, []).append(rule["alert"])
    return alerts


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
    rules: dict[str, list[str]],
    held: dict[str, str] | str,
    suppressed: dict[str, str] | str,
    now: float,
    only: str = "",
) -> tuple[str, int]:
    """The text view and the exit code.

    `held` is the streak notes, or why Loki went unread. `suppressed` maps each check a dead
    exporter skips to that exporter's job, or says why `up` went unread.
    """
    unread_up = suppressed if isinstance(suppressed, str) else None
    skipped_by = suppressed if isinstance(suppressed, dict) else {}
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
            if name in skipped_by:
                state = "skipped"
                notes.append(f"skipped: exporter {skipped_by[name]} down")
            elif gate_row is not None and gate_row.value == 0:
                state = "skipped"
                notes.append(f"skipped: gate {gate} down")
            else:
                state = "down"
                notes.append(
                    "down"
                    + (f": {', '.join(sorted(row.details))}" if row.details else "")
                )
                notes += [f"rule {alert}" for alert in rules.get(name, [])]
        else:
            state = "up"
            if isinstance(held, dict) and name in held:
                notes.append(held[name])
        age = None if row.last_run is None else int(now - row.last_run)
        if age is None or (row.deadline is not None and age > row.deadline):
            notes.append(f"stale: past {int(row.deadline or 0)}s")
            if state == "up":
                state = "stale"
        if name not in table_names:
            notes.append("not in this checkout's check table, pull to read its rule")
        elif name not in rules:
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
    if unread_up:
        out.append(
            f"  exporter state unread, so no row reads skipped by one: {unread_up}"
        )
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


def _suppressed(base, pin, dependent, fetch) -> dict[str, str] | str:
    jobs = "|".join(sorted(dependent))
    try:
        data = fetch(prom_query_url(base, f'up{{job=~"{jobs}"}}'), resolve=pin)
    except core.Unreadable as exc:
        return exc.message
    if data.get("status") != "success":
        return f"Prometheus answered {data.get('error') or data.get('status')!r}"
    return suppressed_checks(data["data"]["result"], dependent)


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
    dependent = exporter_dependent(CHECK_MODULE.read_text())
    text, code = report(
        rows,
        table_gates=gates(source),
        table_names=[r["name"] for r in py_table(source, "CHECKS")],
        rules=rules_by_check(RULES.read_text()),
        held=_held(rows, now, fetch),
        suppressed=_suppressed(base, pin, dependent, fetch),
        now=now,
        only=ns.checks,
    )
    print(text)
    return code
