"""`probe.py landing-verdicts [<pr>]` — the newest recorded `VERDICT:` line of each landed PR.

Orchestrators read a landing's verdict by grepping the harness's background-task output files,
over 60 times in five sessions. Those files are harness internals and disappear with
the session. This reads `land_lib/verdict_log.py`'s record instead, which every landing appends
to as it exits.

BOTH HOSTS, ONE CALL. The record is per user and per host, and a fan-out batch on
daniel-server lands its own PR there. So one call reads this host's file and the file of
every other host in `LANDING_HOSTS`, each through one `host_reader.read_host`, which is one
ssh call however many PRs are asked about. Each row names the host that recorded it, and a PR
both hosts recorded shows its newer record. The read of another host is the file of the user
ssh logs in as, at the default path: `$LAND_VERDICT_LOG` and `$XDG_STATE_HOME` move only this
host's file.

A HOST THAT DOES NOT ANSWER IS NOT AN EMPTY RECORD. The rows that were read still print, and
a `note:` line names the host that was not. A PR with no row among the hosts that answered
then has no answer: the command says so and exits 75, never "no verdict".

Without `<pr>` the first line counts the newest `--limit` PRs by host and by exit code, and
the rows below it are the landings that exited non-zero. `--all` lists every one.

Exit code: 1 when `<pr>` names a PR no host has a record of, 75 when a host that might hold
it did not answer, and 0 otherwise.
"""

import json
import socket
import time

from deploy_tools.land_lib import verdict_log
from diagnostics.probe_lib import core, host_reader
from lib.exit_codes import PROBE_FAILED, PROBE_OK, PROBE_UNREADABLE
from lib.inventory_lists import PI_HOST, host_names

VERDICTS_HELP = (
    "the newest recorded VERDICT line of each PR landed on either k3s host, with the host "
    "that recorded it; exit 1 when PR has no record, 75 when a host that might hold it did "
    "not answer"
)
# The hosts a landing runs on. The Pi runs none.
LANDING_HOSTS = tuple(h for h in host_names() if h != PI_HOST)
# Where `verdict_log.default_path` puts the record for a user with no override set.
REMOTE_PATH = "~/.local/state/homelab/landing-verdicts.jsonl"
READ_TIMEOUT_S = 20


def add_verdicts_args(verdicts) -> None:
    """Add `landing-verdicts`' arguments to `verdicts`, the subparser cli_parser.py made."""
    verdicts.add_argument("pr", nargs="?", help="only this PR's newest verdict")
    verdicts.add_argument(
        "--limit", type=int, default=20, help="PRs to count without PR (default 20)"
    )
    verdicts.add_argument(
        "--all",
        action="store_true",
        help="list every counted PR, not only the landings that exited non-zero",
    )
    verdicts.add_argument("--json", action="store_true", help="the records as JSON")


def read_other(host: str, read_host=host_reader.read_host) -> list[dict]:
    """The newest record of each PR in `host`'s file, read over ssh; none when it has no file.

    Raises:
        core.Unreadable: the host did not answer, or the file exists and could not be read.
    """
    doc = read_host({"files": [REMOTE_PATH]}, host, timeout=READ_TIMEOUT_S)
    got = (doc.get("files") or {}).get(REMOTE_PATH) or {}
    if got.get("status") == "absent":
        return []
    if got.get("status") != "ok":
        raise core.Unreadable(
            f"could not read {REMOTE_PATH} on {host}: {got.get('error') or 'no status'}"
        )
    return verdict_log.parse(got.get("text") or "")


def gather(here: str, others=read_other) -> tuple[list[dict], list[str]]:
    """Every PR's newest record across the landing hosts, and the hosts that did not answer.

    Args:
        here: this host's short name, whose file is read directly.
        others: reads another host's records, as `read_other` does.

    Returns:
        The records newest first, each carrying the `host` that recorded it, and the names
        of the hosts whose read failed.
    """
    rows = [{**e, "host": here} for e in verdict_log.latest()]
    unread = []
    for host in LANDING_HOSTS:
        if host == here:
            continue
        try:
            rows += [{**e, "host": host} for e in others(host)]
        except core.Unreadable:
            unread.append(host)
    newest: dict[str, dict] = {}
    for row in sorted(rows, key=lambda e: e["at"]):
        newest[row["pr"]] = row
    return sorted(newest.values(), key=lambda e: e["at"], reverse=True), unread


def format_row(entry: dict) -> str:
    """`#<pr>  <UTC time>  rc=<n>  <host>  <line>`, one landing to a line."""
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(entry["at"]))
    return (
        f"#{entry['pr']}  {stamp}  rc={entry['rc']}  {entry['host']}  {entry['line']}"
    )


def _count_line(entries: list[dict], listed: int, show_all: bool) -> str:
    by_host: dict[str, int] = {}
    for e in entries:
        by_host[e["host"]] = by_host.get(e["host"], 0) + 1
    hosts = ", ".join(f"{n} on {h}" for h, n in sorted(by_host.items()))
    failed = sum(1 for e in entries if e["rc"] != 0)
    line = f"{len(entries)} landing verdict(s)" + (f": {hosts}" if hosts else "")
    line += f"; {len(entries) - failed} exited 0, {failed} did not"
    if listed < len(entries) and not show_all:
        line += "; --all lists every row"
    return line


def run_landing_verdicts(ns, here: str = "", others=read_other) -> int:
    """Print the newest record of each PR, newest first, or of `ns.pr` alone; the exit code."""
    here = here or socket.gethostname().split(".")[0]
    entries, unread = gather(here, others)
    pr = ns.pr.lstrip("#") if ns.pr else ""
    entries = [e for e in entries if e["pr"] == pr] if pr else entries[: ns.limit]
    missing = bool(pr) and not entries
    rc = (PROBE_UNREADABLE if unread else PROBE_FAILED) if missing else PROBE_OK
    if ns.json:
        print(json.dumps(entries))
        return rc
    read = [h for h in dict.fromkeys((here, *LANDING_HOSTS)) if h not in unread]
    lines = []
    if pr:
        lines += [format_row(e) for e in entries]
        if missing and unread:
            lines.append(
                f"PR #{pr} has no landing verdict on {' or '.join(read)}, and that is not "
                "an answer while a host is unread"
            )
        elif missing:
            lines.append(
                f"no landing verdict recorded for PR #{pr} on {' or '.join(read)}"
            )
    else:
        shown = entries if ns.all else [e for e in entries if e["rc"] != 0]
        lines.append(_count_line(entries, len(shown), ns.all))
        lines += [format_row(e) for e in shown]
    lines += [
        f"note: {host} did not answer, so its landing verdicts are not shown"
        for host in unread
    ]
    print("\n".join(lines))
    return rc
