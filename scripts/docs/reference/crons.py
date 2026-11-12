#!/usr/bin/env python3
"""Generate docs/reference/crons.md — every systemd timer the host packages ship.

WHY THIS PAGE IS WORTH HAVING. Some of these jobs commit, push, deploy or delete without a
human in the loop. That set is the reason to read the page, and it is not obvious from any
single component: the timers are spread across the host packages.

ONE SOURCE. Each row comes from the rendered host packages
(`docs.catalog_lib.package_timers`), so it shows what each host receives. No role installs a
cron: the roles render k8s manifests only, and a k8s CronJob is not listed here.

WHAT IT CANNOT DECIDE. Whether a job changes state is judged from its `ExecStart=` command by
the keyword list below, and a job whose command is a wrapper script is marked as needing the
script read. That is a heuristic and says so on the page; nothing in a unit declares its own
blast radius.

Usage::

    uv run python scripts/docs/reference/crons.py --out docs/reference/crons.md
"""

import re
from pathlib import Path


# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
from pathlib import Path as _Path
import sys as _sys

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
# This directory's secrets.py would shadow the stdlib module the host renderer needs.
if __name__ == "__main__":
    _sys.path.remove(str(_Path(__file__).resolve().parent))

from lib.cli_help import ArgumentParser

from docs.catalog_lib.package_timers import package_timers
from lib.docs_provenance import md_cell

from lib.repo_paths import REPO

HOSTS = REPO / "hosts"

# Commands that change durable state somewhere. Deliberately over-broad: a job wrongly
# flagged as state-changing costs a reader one moment, and one wrongly cleared could cost
# an incident.
_MUTATING = (
    "git commit",
    "git push",
    "deploy",
    "rotate",
    "rm ",
    "delete",
    "prune",
    "truncate",
    "restart",
    "apply",
    "backup",
    "snapshot",
    "reboot",
)


def _changes_state(job: str) -> str:
    lowered = job.lower()
    hits = [word.strip() for word in _MUTATING if word in lowered]
    if hits:
        return f"yes ({', '.join(sorted(set(hits)))})"
    if ".sh" in lowered:
        return "read the script"
    return "no (read-only by its command)"


_SPECIAL_CADENCE_MINUTES = {
    "yearly": 525600.0,
    "annually": 525600.0,
    "monthly": 43200.0,
    "weekly": 10080.0,
    "daily": 1440.0,
    "midnight": 1440.0,
    "hourly": 60.0,
}


def cadence_minutes(schedule: str) -> float | None:
    """The approximate interval between runs a rendered five-field `schedule` implies.

    Reads the shape of the schedule only -- whether each field is `*`, a `*/N` step, or
    anything else -- never a field's literal value. That is what lets it work on a schedule
    this page has already printed as unresolved Jinja (`{{ some_var }}`): a templated field
    is simply "not a wildcard", which is enough to place it in the right cadence bucket
    without knowing what it resolves to.

    Returns None when the shape gives no honest answer (`@reboot`, or a schedule this
    heuristic does not recognize).
    """
    schedule = schedule.strip()
    if schedule.startswith("@"):
        return _SPECIAL_CADENCE_MINUTES.get(schedule[1:])
    fields = schedule.split()
    if len(fields) != 5:
        return None
    minute, hour, day, month, weekday = fields

    def _step(field: str) -> int | None:
        if not field.startswith("*/"):
            return None
        try:
            return int(field[2:])
        except ValueError:
            return None

    minute_step = _step(minute)
    if minute_step and hour == "*":
        return float(minute_step)
    hour_step = _step(hour)
    if hour_step and minute != "*":
        return float(hour_step * 60)
    if minute != "*" and hour == "*":
        return 60.0
    if weekday != "*":
        return 10080.0
    if day != "*":
        return 43200.0 if month == "*" else 525600.0
    if hour != "*" and "," in hour:
        return 1440.0 / len(hour.split(","))
    if hour != "*" and minute != "*":
        return 1440.0
    return None


def _timer_changes_state(service: str) -> str:
    """The state verdict for a timer's service, never "no".

    An `ExecStart=` usually names a wrapper under `/usr/lib/homelab/`, whose name says nothing
    about what it does, so a command with no mutating keyword still needs the script read.
    """
    command = " ".join(
        line.partition("=")[2]
        for line in service.splitlines()
        if line.startswith("ExecStart")
    )
    verdict = _changes_state(command)
    return "read the script" if verdict.startswith("no") else verdict


def build_timer_rows(hosts_root: Path = HOSTS) -> list[dict[str, str]]:
    """One row per timer the host packages ship, hosts that agree on it sharing the row.

    Args:
        hosts_root: The `hosts/` tree to render.

    Returns:
        One dict per timer and schedule, keyed by the columns rendered in `render_markdown`.
    """
    grouped: dict[tuple[str, ...], list[str]] = {}
    for timer in package_timers(hosts_root):
        schedule = ", ".join(
            f"{key}={value}"
            for key, values in timer.triggers.items()
            for value in values
        )
        user = re.search(r"^User=(.+)$", timer.service, re.M)
        key = (
            timer.unit,
            schedule,
            user.group(1) if user else "root",
            _timer_changes_state(timer.service),
            timer.source,
        )
        host = timer.host if timer.enabled else f"{timer.host} (no enable link)"
        grouped.setdefault(key, []).append(host)
    return [
        {
            "name": unit,
            "schedule": schedule,
            "host": ", ".join(hosts),
            "user": user,
            "changes_state": changes_state,
            "source": source,
        }
        for (unit, schedule, user, changes_state, source), hosts in grouped.items()
    ]


def render_markdown(rows: list[dict[str, str]]) -> str:
    """Render `rows` as the "Scheduled jobs" reference page, banner and schedule notes included.

    Args:
        rows: Timer rows as returned by `build_timer_rows`.

    Returns:
        The full page as Markdown text, ending in a single trailing newline.
    """
    from lib.docs_provenance import generated_banner

    parts = [generated_banner("scripts/docs/reference/crons.py")]
    parts.append("# Scheduled jobs\n")
    parts.append(f"{len(rows)} job(s) scheduled by the host packages' timers.\n")
    parts.append(
        '!!! warning "The state column is a heuristic"\n'
        "    It is judged from the command text, and nothing in a unit declares its "
        'own blast radius. A job that runs a wrapper script reads as "read the script" '
        "rather than being guessed at. Treat it as a pointer, not an authority.\n"
    )

    parts.append("| Job | Schedule | Host | User | Changes state | Defined in |")
    parts.append("|---|---|---|---|---|---|")
    for row in sorted(rows, key=lambda r: r["name"]):
        parts.append(
            f"| {md_cell(row['name'])} | `{row['schedule']}` | {md_cell(row['host'])} | "
            f"`{row['user']}` | {row['changes_state']} | `{row['source']}` |"
        )

    parts.append(
        "\n## Schedule format\n\n"
        "A host package's timer shows its triggers as systemd reads them, each host's "
        "drop-ins applied. A host marked `(no enable link)` receives the unit and does not "
        "arm it.\n"
    )
    return "\n".join(parts).rstrip("\n") + "\n"


def main(argv: list[str] | None = None) -> int:
    """Build the timer rows, render the reference page, and write it if the body changed.

    Returns:
        The exit code from `finish_generator` (0 on success, non-zero on a write failure).
    """
    parser = ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True, help="output file path")
    args = parser.parse_args(argv)

    from lib.docs_provenance import finish_generator

    rows = build_timer_rows()
    return finish_generator(
        "docs.reference.crons", args.out, rows, render_markdown, "job"
    )


if __name__ == "__main__":
    raise SystemExit(main())
