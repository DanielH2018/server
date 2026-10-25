"""`probe.py landing-verdicts [<pr>]` — the newest recorded `VERDICT:` line of each landed PR.

Orchestrators read a landing's verdict by grepping the harness's background-task output files,
over 60 times in five sessions (#6325). Those files are harness internals and disappear with
the session. This reads `land_lib/verdict_log.py`'s record instead, which every landing appends
to as it exits. The record is this user's on this host: a landing run on daniel-box reads back
only there.

Exit code: 1 when `<pr>` names a PR with no record, and 0 otherwise.
"""

import json
import sys as _sys
from pathlib import Path as _Path

# `land_lib` sits under `scripts/deploy_tools/`, which a directly-invoked probe.py does not
# have on sys.path. This has to sit ABOVE the import below.
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2] / "deploy_tools"))

from land_lib import verdict_log


def add_verdicts_parser(sub) -> None:
    """Add the `landing-verdicts` subparser to probe.py's subparsers `sub`."""
    verdicts = sub.add_parser(
        "landing-verdicts",
        help=(
            "the newest recorded VERDICT line of each PR this user landed on this host, "
            "newest first; exit 1 when PR has no record"
        ),
    )
    verdicts.add_argument("pr", nargs="?", help="only this PR's newest verdict")
    verdicts.add_argument(
        "--limit", type=int, default=20, help="PRs to list without PR (default 20)"
    )
    verdicts.add_argument("--json", action="store_true", help="the records as JSON")


def run_landing_verdicts(ns) -> int:
    """Print the newest record of each PR, newest first, or of `ns.pr` alone; the exit code."""
    entries = verdict_log.latest()
    if ns.pr:
        entries = [e for e in entries if e["pr"] == ns.pr.lstrip("#")]
    else:
        entries = entries[: ns.limit]
    if ns.json:
        print(json.dumps(entries))
    elif entries:
        print("\n".join(verdict_log.format_row(e) for e in entries))
    else:
        what = f"PR #{ns.pr.lstrip('#')}" if ns.pr else "any PR"
        print(f"no landing verdict recorded for {what} in {verdict_log.default_path()}")
    return 1 if ns.pr and not entries else 0
