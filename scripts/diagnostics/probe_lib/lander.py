"""`probe.py lander` — what the daniel-server batch lander did with each PR, and how it ended.

Sessions read `journalctl -u claude-fanout-server-lander` by hand, 54 times in two weeks, to
learn what the lander did with a wave's PRs (#6324). This makes one read of daniel-box and
prints:

- the newest pass: when it ran, how its unit run ended, and its one line per candidate PR,
  `eligible` or `ineligible` with the reason. That is the queue;
- each landing the lander started in the window: its log under the lander's state directory,
  whether it is still running, its exit code and its `VERDICT:` line;
- every other PR a pass in the window named, with the newest line about it.

`--pr N` narrows every section to one PR.

The verdict is about the lander, not the PRs: a landing's `pr-ci-red` is the PR's verdict. Exit
1 when the unit is not installed, the newest run failed, no pass ran within two timer periods
plus `TimeoutStartSec`, or the newest pass could not read daniel-server or GitHub. Both values
come from the role's defaults and unit template. 75 when daniel-box or the journal could not be
read. The landing logs sit under the operator's home, which the agent user cannot list; that
section then says so and the verdict stands on the journal alone.
"""

# `probe_lib` is a namespace package under `scripts/`, so reaching a sibling by package name
# needs `scripts/` on sys.path — a module gets only its importer's path otherwise, and
# pyproject's `pythonpath` is a pytest setting. This has to sit ABOVE the imports below.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import re
import time

from diagnostics.probe_lib import core
from diagnostics.probe_lib.host_reader import (
    Entry,
    BOX,
    dry_run_text,
    journal_entries,
    read_host,
    unit_span_s,
)
from lib import yaml_fast
from lib.deployer_park import _age_phrase
from lib.exit_codes import PROBE_FAILED, PROBE_OK
from lib.repo_paths import ANSIBLE
from lib.systemd_timer import span_s

NAME = "claude-fanout-server-lander"
UNIT = f"{NAME}.service"
ROLE = ANSIBLE / "roles" / "setup" / "claude_code"
GROUP_VARS = ANSIBLE / "inventory" / "group_vars" / "all.yml"
SHOW = ["LoadState", "ActiveState", "Result", "ExecMainExitTimestamp"]
# `fanout_lib/autoland.py:run` prints these when a pass read one source and not the other.
UNREAD = ("records unreadable", "GitHub unreadable")

LANDER_HELP = (
    "the daniel-server batch lander on daniel-box: the newest pass with its eligible and "
    "ineligible line per PR, each landing it started with its VERDICT, and every other PR "
    "the window named",
    "the newest pass ran on time and read both sources",
    "the lander is not installed, its newest run failed or is stale, or a source was unread",
)

_PR = re.compile(r"^#(\d+) ")
_LOG = re.compile(r"^land(\d+)-")


def settings(role=ROLE, group_vars=GROUP_VARS) -> tuple[int, int, str]:
    """(timer period, run ceiling, landing log directory), read from the role and inventory."""
    defaults = yaml_fast.safe_load((role / "defaults" / "main.yml").read_text())
    unit = (role / "templates" / f"{UNIT}.j2").read_text()
    user = yaml_fast.safe_load(group_vars.read_text())["sys_user"]
    period = span_s(defaults["claude_code_fanout_server_lander_interval"])
    ceiling = unit_span_s(unit, "TimeoutStartSec")
    return period, ceiling, f"/home/{user}/.local/state/fanout-server-lander/logs"


def plan(since: str, since_epoch: float, log_dir: str) -> dict:
    return {
        "journal": {"lander": [UNIT, since, None]},
        "show": {UNIT: SHOW},
        "logs": [log_dir, "land*-*.log", since_epoch],
    }


def passes(entries) -> list[list]:
    """The lander's own lines, one list per unit run, oldest run first."""
    runs: dict[str, list] = {}
    for e in entries:
        if e.identifier != "systemd" and e.invocation:
            runs.setdefault(e.invocation, []).append(e)
    return sorted(runs.values(), key=lambda run: run[0].at)


def _stamp(at: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(at))


def _pr_of(message: str) -> int | None:
    m = _PR.match(message)
    return int(m.group(1)) if m else None


def _landings(logs: dict | None, pr: int | None) -> list[str]:
    if not logs or logs.get("status") != "ok":
        status = (logs or {}).get("status", "not read")
        return [
            f"  landing logs {status}{': ' + logs['error'] if logs and logs.get('error') else ''}"
        ]
    out = []
    for row in logs.get("rows") or []:
        m = _LOG.match(row["name"])
        number = int(m.group(1)) if m else None
        if pr is not None and number != pr:
            continue
        rc = (row.get("rc") or "").strip()
        state = "running" if row.get("alive") and not rc else f"ended rc {rc or '?'}"
        verdict = row.get("verdict") or "no VERDICT line yet"
        out.append(f"  #{number}  {row['name']}  {state}  {verdict}")
    return out or ["  none"]


def report(
    doc: dict, *, now: float, period: int, ceiling: int, pr: int | None = None
) -> tuple[str, int]:
    """The text view and the exit code for one read of daniel-box."""
    entries = journal_entries(doc, "lander")
    unit = (doc.get("show") or {}).get(UNIT) or {}
    if unit.get("LoadState") != "loaded":
        return (
            f"lander: {UNIT} is {unit.get('LoadState') or 'unknown'} on {doc.get('host')} "
            "(claude_code_fanout_server_lander_enabled is false there?)",
            PROBE_FAILED,
        )
    runs = passes(entries)
    problems = []
    if unit.get("Result") not in ("success", None):
        problems.append(f"the newest run ended {unit['Result']}")
    deadline = 2 * period + ceiling
    newest = runs[-1] if runs else []
    if not newest:
        problems.append("no pass in the window")
    else:
        age = int(now - newest[0].at)
        if age > deadline:
            problems.append(
                f"the newest pass is {_age_phrase(age)} old, past {deadline}s"
            )
        problems += [e.message for e in newest if e.message.startswith(UNREAD)]
    lines = [
        f"lander ({doc.get('host')}): {unit.get('ActiveState')}, newest run "
        f"{unit.get('Result')}; timer every {period}s"
    ]
    if newest:
        lines.append(
            f"newest pass {_stamp(newest[0].at)} ({_age_phrase(int(now - newest[0].at))} ago):"
        )
        lines += [
            f"  {e.message}"
            for e in newest
            if pr is None or _pr_of(e.message) in (pr, None)
        ]
    lines.append("landings started in the window:")
    lines += _landings(doc.get("logs"), pr)
    seen = {_pr_of(e.message) for e in newest}
    latest: dict[int, Entry] = {}
    for run in runs[:-1]:
        for e in run:
            number = _pr_of(e.message)
            if number is not None and number not in seen and pr in (None, number):
                latest[number] = e
    lines.append("other PRs the window named, newest line each:")
    lines += [
        f"  {_stamp(latest[n].at)}  {latest[n].message}" for n in sorted(latest)
    ] or ["  none"]
    lines.append(f"verdict: {'; '.join(problems) or 'landing on schedule'}")
    return "\n".join(lines), PROBE_FAILED if problems else PROBE_OK


def run_lander(ns, read=read_host, now: float | None = None) -> int:
    """Read daniel-box once, print the report, return its exit code."""
    now = time.time() if now is None else now
    window = core.parse_duration_seconds(ns.since)
    period, ceiling, log_dir = settings()
    request = plan(f"-{window}s", now - window, log_dir)
    if ns.dry_run:
        print(dry_run_text(request, BOX))
        return PROBE_OK
    text, code = report(
        read(request, BOX), now=now, period=period, ceiling=ceiling, pr=ns.pr
    )
    print(text)
    return code


def add_parser(sub, verdict_help) -> None:
    """Add `lander` to probe.py's subparsers; it lives here while cli_parser.py sits at its cap."""
    lp = sub.add_parser("lander", **verdict_help(*LANDER_HELP))
    lp.add_argument("--pr", type=int, help="narrow every section to this PR")
    lp.add_argument("--since", default="6h", help="journal window, e.g. 2h or 1d")
    lp.add_argument(
        "--dry-run", action="store_true", help="print the read without making it"
    )
