#!/usr/bin/env python3
"""Post one Discord summary of the past week's unattended Claude runs.

homelab-claude-weekly-summary.timer runs this as root on daniel-box every Monday
at 09:00 UTC. A run covers the ISO week before its own, Monday 00:00 to Sunday 23:59 UTC, so a
catch-up run on a later day still covers that week. It reads each source the config lists:

- `triage`: a directory of `<episode>.json` run records, which `claude_triage_run.py` writes;
- `renovate`: a `runs.jsonl` file of one record per tick, which `agent_logic.run_record` writes.

The post leads with one count line per source: runs, sessions spent, total cost and total
denials. It then lists each triage run, each Renovate tick that merged, closed or left PRs open
or did not finish, and every run with a permission denial. A week with no runs still posts its
count lines. Text a session or an alert wrote is defused of Discord mentions and clamped.

The posted week is recorded in the state directory, so a second run for the same week posts
nothing. A missing source reads as zero runs.

Usage: claude_weekly_summary.py CONFIG
Exit 0 = up, 1 = down. Each run logs one `status=<up|down>` line under the
claude-weekly-summary tag and writes claude-weekly-summary.prom to TEXTFILE_DIR.
"""

import datetime as dt
import json
import os
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from host_lib import discord_post, read_untrusted, write_check_textfile, write_textfile

CHECK = "claude-weekly-summary"
MARKER = "claude-weekly-summary:"
TRIAGE_RECORD_MAX = 1 << 20
RENOVATE_LOG_MAX = 16 << 20
LINE_MAX = 300
SUMMARY_MAX = 160
TRIAGE_TIME = "%Y-%m-%d %H:%M UTC"
# A zero-width space after `<` breaks `<@id>`, `<@&id>` and `<#id>`; after `@`, `@everyone` and `@here`.
_MENTION = re.compile(r"<(?=[@#])|@(?!\u200b)")


@dataclass
class Tally:
    """One source's runs inside the window."""

    name: str
    runs: int = 0
    sessions: int = 0
    cost_usd: float = 0.0
    denials: int = 0
    unreadable: int = 0
    error: str = ""
    items: list[str] = field(default_factory=list)


def week_window(now: float) -> tuple[dt.datetime, dt.datetime]:
    """The ISO week before the one holding `now`: its Monday 00:00 UTC and the next Monday."""
    today = dt.datetime.fromtimestamp(now, dt.UTC).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    this_monday = today - dt.timedelta(days=today.weekday())
    return this_monday - dt.timedelta(days=7), this_monday


def defuse(text: object, limit: int = LINE_MAX) -> str:
    """`text` on one line, with no Discord mention or channel link, cut to `limit` characters."""
    flat = " ".join(str(text).split())
    flat = _MENTION.sub(lambda m: m.group(0) + "\u200b", flat)
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _number(value: object) -> float:
    return (
        value if isinstance(value, int | float) and not isinstance(value, bool) else 0
    )


def _prs(numbers: object) -> str:
    if not isinstance(numbers, list):
        return ""
    return ", ".join(f"#{n}" for n in numbers if isinstance(n, int))


def _denials(count: int) -> str:
    return f"; {count} denial{'s' * (count != 1)}" if count else ""


def read_triage(path: str, start: float, end: float, tally: Tally) -> None:
    """Count the triage records in `path` whose run started inside [start, end)."""
    try:
        names = sorted(n for n in os.listdir(path) if n.endswith(".json"))
    except FileNotFoundError:
        return
    for name in names:
        try:
            record = json.loads(read_untrusted(path, name, TRIAGE_RECORD_MAX))
            started = (
                dt.datetime.strptime(record["started"], TRIAGE_TIME)
                .replace(tzinfo=dt.UTC)
                .timestamp()
            )
        except ValueError, KeyError, TypeError:
            tally.unreadable += 1
            continue
        if not start <= started < end:
            continue
        tally.runs += 1
        state = str(record.get("state"))
        if state != "capped":
            tally.sessions += 1
        tally.cost_usd += _number(record.get("cost_usd"))
        denials = int(_number(record.get("denials")))
        tally.denials += denials
        episode = (
            record.get("episode") if isinstance(record.get("episode"), dict) else {}
        )
        if episode.get("storm"):
            subject = f"storm {record.get('episode_id')}"
        elif episode.get("monitor"):
            subject = f"{episode['monitor']}:{episode.get('service', '')}"
        else:
            subject = str(record.get("episode_id"))
        diagnosis = (
            record.get("diagnosis") if isinstance(record.get("diagnosis"), dict) else {}
        )
        summary = diagnosis.get("summary") or record.get("error") or ""
        line = f"- {defuse(subject, 80)} {defuse(state, 20)}{_denials(denials)}"
        if summary:
            line += f": {defuse(summary, SUMMARY_MAX)}"
        if record.get("report"):
            line += f" <{defuse(record['report'], 200)}>"
        tally.items.append(defuse(line))


def read_renovate(path: str, start: float, end: float, tally: Tally) -> None:
    """Count the Renovate ticks in the `path` log whose `ts` falls inside [start, end)."""
    directory, name = os.path.split(path)
    try:
        text = read_untrusted(directory, name, RENOVATE_LOG_MAX)
    except FileNotFoundError:
        return
    for raw in text.splitlines():
        if not raw.strip():
            continue
        try:
            record = json.loads(raw)
            ts = float(record["ts"])
        except ValueError, KeyError, TypeError:
            tally.unreadable += 1
            continue
        if not start <= ts < end:
            continue
        tally.runs += 1
        if "turns" in record:
            tally.sessions += 1
        tally.cost_usd += _number(record.get("cost_usd"))
        denials = int(_number(record.get("denials")))
        tally.denials += denials
        result = str(record.get("result"))
        moved = [
            f"{label} {_prs(record.get(key))}"
            for key, label in (
                ("merged", "merged"),
                ("closed", "closed"),
                ("left_open", "left open"),
            )
            if _prs(record.get(key))
        ]
        if not (moved or denials or result in ("failed", "crashed")):
            continue
        when = dt.datetime.fromtimestamp(ts, dt.UTC).strftime("%a %m-%d")
        detail = "; ".join(moved) or defuse(record.get("reason") or "", SUMMARY_MAX)
        tally.items.append(
            defuse(f"- {when} {defuse(result, 20)}{_denials(denials)}: {detail}")
        )


READERS: dict[str, Callable[[str, float, float, Tally], None]] = {
    "triage": read_triage,
    "renovate": read_renovate,
}


def render(week: str, start: dt.datetime, host: str, tallies: list[Tally]) -> str:
    """The post: a heading, one count line per source, then each source's listed runs."""
    last_day = (start + dt.timedelta(days=6)).date()
    lines = [f"{MARKER} {week} ({start.date()} to {last_day} UTC) on {host}"]
    for t in tallies:
        line = (
            f"{t.name}: {t.runs} run{'s' * (t.runs != 1)}, {t.sessions} session"
            f"{'s' * (t.sessions != 1)}, ${t.cost_usd:.2f}, {t.denials} denial"
            f"{'s' * (t.denials != 1)}"
        )
        if t.unreadable:
            line += f" ({t.unreadable} unreadable record{'s' * (t.unreadable != 1)})"
        if t.error:
            line += f" (source unreadable: {defuse(t.error, 120)})"
        lines.append(line)
    for t in tallies:
        if t.items:
            lines.append(f"{t.name}:")
            lines.extend(t.items)
    return "\n".join(lines)


def run(
    config: dict, now: float, host: str, post: Callable[[str], bool]
) -> tuple[bool, str]:
    """Summarise and post the week before `now` once; (up, the status line's detail)."""
    start, end = week_window(now)
    week = start.strftime("%G-W%V")
    state = Path(config["state_dir"])
    record_path = state / "posted.json"
    if record_path.exists() and json.loads(record_path.read_text()).get("week") == week:
        return True, f"{week} already posted"
    tallies = []
    for source in config["sources"]:
        tally = Tally(source["name"])
        try:
            READERS[source["kind"]](
                source["path"], start.timestamp(), end.timestamp(), tally
            )
        except OSError as exc:
            tally.error = f"{exc.strerror or exc} ({source['path']})"
        tallies.append(tally)
    if not post(render(week, start, host, tallies)):
        return False, f"{week}: the Discord post failed; the next run retries it"
    state.mkdir(parents=True, exist_ok=True)
    write_textfile(record_path, json.dumps({"week": week}) + "\n", 0o600)
    runs = sum(t.runs for t in tallies)
    unreadable = [t.name for t in tallies if t.error]
    if unreadable:
        return False, f"{week} posted with {', '.join(unreadable)} unreadable"
    return True, f"{week} posted: {runs} run{'s' * (runs != 1)}"


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] in ("-h", "--help"):
        print(__doc__)
        return 0 if argv[1:] in (["-h"], ["--help"]) else 2
    config = json.loads(Path(argv[1]).read_text())
    webhook = (
        Path(os.environ["CREDENTIALS_DIRECTORY"], "discord_webhook").read_text().strip()
    )

    def post(content: str) -> bool:
        return discord_post(webhook, content, "homelab-claude-weekly-summary")

    started = time.time()
    try:
        ok, line = run(config, started, os.uname().nodename, post)
    except (OSError, ValueError, KeyError) as exc:
        ok, line = False, f"run failed: {exc}"
    print(f"status={'up' if ok else 'down'} {line}", flush=True)
    textfile_dir = os.environ.get("TEXTFILE_DIR", "/var/lib/node-exporter-textfile")
    write_check_textfile(
        f"{textfile_dir}/{CHECK}.prom",
        CHECK,
        success=ok,
        deadline_s=config["deadline_s"],
        started=started,
    )
    return 0 if ok else 1


if __name__ == "__main__":
    os.umask(0o077)
    sys.exit(main(sys.argv))
