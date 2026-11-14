"""`probe.py uptime-robot` — do the Uptime Robot monitors the docs name still exist and run?

The monitors were created by hand in the Uptime Robot console, and
`docs/uptime-robot-monitors.md` is the only record of them. A monitor deleted or paused there
leaves no trace in git: `803270234`, the disaster-recovery backstop two runbooks named, was
deleted at a time nothing recorded (#8525). This reads the account's monitors back through
Uptime Robot's API and fails when a documented id is missing or paused.

The doc's *What is configured* table is the oracle, parsed here and nowhere else:
`tests/repo/test_uptime_robot_keywords.py` reads the same rows to check each keyword against
the live endpoint.

The API key is the SOPS key `uptimerobot_api_key`, fed to curl on stdin so it never reaches
argv. Uptime Robot issues a read-only key that can list monitors and change nothing; that is
the only kind this check needs.
"""

import re

# `probe_lib` is a namespace package under `scripts/`, so reaching a sibling by package name
# needs `scripts/` on sys.path — a module gets only its importer's path otherwise, and
# pyproject's `pythonpath` is a pytest setting. This has to sit ABOVE the imports below.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

# `core.<name>` for anything the tests monkeypatch — binding those into this module's
# globals with a `from core import ...` would take a snapshot the patch never reaches.
from diagnostics.probe_lib import core

from lib.repo_paths import REPO

DOC = REPO / "docs" / "uptime-robot-monitors.md"

# One row of the *What is configured* table: `| name | `id` | `url` | `keyword` must exist |`.
_ROW = re.compile(
    r"^\| (?P<name>[^|]+?) \| `(?P<id>\d+)` \| `(?P<url>https://[^`]+)` \|"
    r" `(?P<keyword>[^`]+)` must (?P<rule>exist|not exist) \|$",
    re.MULTILINE,
)

# The monitors the table lists. The parse is a regex over prose, so a reworded table would
# otherwise yield no rows and a check with nothing to compare.
KNOWN_MONITORS = frozenset({"Auth Health", "Jellyfin Health"})

API_URL = "https://api.uptimerobot.com/v2/getMonitors"
API_KEY_NAME = "uptimerobot_api_key"

# v2's `status`: 0 paused, 1 not checked yet, 2 up, 8 seems down, 9 down. Only 0 means the
# console stopped the monitor; a down monitor is still watching, and pages on its own.
PAUSED = 0


def documented_monitors(text: str | None = None) -> list[dict[str, str]]:
    """The rows of the doc's monitor table, each a dict of name, id, url, keyword and rule.

    Raises:
        SystemExit: a monitor in `KNOWN_MONITORS` is not in the table. No retry can fix
          that, so `probe.py` reports it as the fail verdict rather than as unreadable.
    """
    rows = [
        m.groupdict() for m in _ROW.finditer(DOC.read_text() if text is None else text)
    ]
    missing = KNOWN_MONITORS - {row["name"] for row in rows}
    if missing:
        raise SystemExit(f"{DOC.name}: monitor table no longer lists {sorted(missing)}")
    return rows


def api_config(api_key: str) -> str:
    """The `curl --config -` body that POSTs the key, so it never reaches argv.

    It asks for every monitor rather than filtering by id: how the API answers a filter
    naming a deleted id is undocumented, and a deletion must read as a verdict.
    """
    return f'data = "api_key={api_key}&format=json"\n'


def format_uptime_robot(
    documented: list[dict[str, str]], payload: dict
) -> tuple[str, int]:
    """Render the verdict as `(text, exit_code)`: 1 when a documented monitor is missing or paused.

    Raises:
        Unreadable: the API refused the request or returned a partial page, so the monitor
          list proves nothing.
    """
    if payload.get("stat") != "ok":
        raise core.Unreadable(
            f"Uptime Robot refused the monitor list: {payload.get('error')}"
        )
    monitors = payload.get("monitors") or []
    total = (payload.get("pagination") or {}).get("total", len(monitors))
    if total > len(monitors):
        raise core.Unreadable(
            f"Uptime Robot returned {len(monitors)} of {total} monitors; a partial page "
            "cannot show that a monitor is missing"
        )
    live = {str(m.get("id")): m for m in monitors}
    problems = []
    for row in documented:
        monitor = live.get(row["id"])
        if monitor is None:
            problems.append(f"  missing  {row['id']}  {row['name']}: no such monitor")
        elif monitor.get("status") == PAUSED:
            problems.append(f"  paused   {row['id']}  {row['name']}")
    ok = len(documented) - len(problems)
    lines = [f"{ok}/{len(documented)} documented monitors exist and are not paused"]
    if problems:
        lines += problems
        lines.append(
            f"FAIL: {DOC.relative_to(REPO)} names a monitor the console no longer runs. "
            "Restore it at https://dashboard.uptimerobot.com, or update the doc and every "
            "runbook that cites the id."
        )
    return "\n".join(lines), 1 if problems else 0


def run_uptime_robot(ns, *, read_key=None, post=None) -> int:
    """List the documented monitors through the API and judge them (read-only).

    Args:
        ns: the parsed arguments; only `dry_run` is read.
        read_key: returns the named secret; defaults to `core.sops_extract`.
        post: sends `(url, curl config body)` and returns the response body; defaults to
          `core.config_get`.
    """
    documented = documented_monitors()
    if getattr(ns, "dry_run", False):
        print(
            " ".join(core.ha_curl_argv(API_URL))
            + f"   # + api_key=<{API_KEY_NAME}> (via --config stdin)"
        )
        return 0
    api_key = (read_key or core.sops_extract)(API_KEY_NAME)
    body = (post or core.config_get)(API_URL, api_config(api_key))
    text, code = format_uptime_robot(documented, core.parse_json(API_URL, body))
    print(text)
    return code
