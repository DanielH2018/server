"""Every landing's verdict, one JSON line per landing, in a file that outlives the session.

WHY IT EXISTS (issue #6325). A landing prints its `VERDICT:` line to stdout, and the only
places that line persisted were the caller's: a `--detach` log under `$CLAUDE_JOB_DIR/tmp`, a
fan-out worktree's `.fanout/`, or the harness's background-task output file. All three vanish
with the session or the worktree, and the third is a harness internal. Between 2026-10-12 and
2026-10-24, five orchestrator sessions grepped those task files for `VERDICT` over 60 times.
`probe.py landing-verdicts [<pr>]` reads this file instead.

THE WRITERS. `land.py:_land` records every landing it runs, plain or `--detach`, the crash
path included, beside the `--verdict-file` write and the Landings annotation. `land.py:_hand_off`
records the line a lander unit reported, because the unit lands as the operator and writes the
operator's file, which the `claude` user cannot read. A write that fails is suppressed: a
landing must never report failure because recording it did not.

WHERE. `$LAND_VERDICT_LOG` when set, which is how the deploy-tools tests keep a landing out of
the real file. Otherwise `$XDG_STATE_HOME/homelab/landing-verdicts.jsonl`, defaulting to
`~/.local/state/`. The file is per user and per host, for the reason `detach.default_log_dir`
gives for its own directory: a shared one created under the operator's umask shut the
`claude` user out. A landing run on daniel-box is therefore readable only on daniel-box.

GROWTH. A write that leaves more than `KEEP * 2` lines trims the file to its newest `KEEP`. At
a few dozen landings a day, `KEEP` covers weeks, and nothing reads further back.

Each line is `{"pr": "<n>", "at": <epoch s>, "rc": <exit>, "line": "<VERDICT or STOPPED line>"}`.
"""

import fcntl
import json
import os
import time
from pathlib import Path

PATH_ENV = "LAND_VERDICT_LOG"
KEEP = 500


def default_path() -> Path:
    """`$LAND_VERDICT_LOG`, else `$XDG_STATE_HOME/homelab/landing-verdicts.jsonl`."""
    override = os.environ.get(PATH_ENV)
    if override:
        return Path(override)
    state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state) / "homelab" / "landing-verdicts.jsonl"


def record(
    pr: str, rc: int, line: str, path: Path | None = None, keep: int = KEEP
) -> None:
    """Append one landing's result; trim to the newest `keep` once past twice that.

    The lock is the file's own `flock`, so two landings finishing together both land a line.
    """
    target = path or default_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    entry = json.dumps({"pr": str(pr), "at": int(time.time()), "rc": rc, "line": line})
    with target.open("a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.write(entry + "\n")
        fh.flush()
        fh.seek(0)
        lines = fh.read().splitlines()
        if len(lines) > keep * 2:
            fh.seek(0)
            fh.truncate()
            fh.write("\n".join(lines[-keep:]) + "\n")


def latest(path: Path | None = None) -> list[dict]:
    """The newest record of each PR, newest first. A missing file is no records.

    A line that does not parse, or lacks a field, is skipped: it is a torn write or another
    tool's line, and one of them must not hide every other PR's verdict.
    """
    target = path or default_path()
    try:
        text = target.read_text(errors="replace")
    except FileNotFoundError:
        return []
    newest: dict[str, dict] = {}
    for raw in text.splitlines():
        try:
            entry = json.loads(raw)
            newest[str(entry["pr"])] = {
                "pr": str(entry["pr"]),
                "at": int(entry["at"]),
                "rc": int(entry["rc"]),
                "line": str(entry["line"]),
            }
        except ValueError, KeyError, TypeError:
            continue
    return sorted(newest.values(), key=lambda e: e["at"], reverse=True)


def format_row(entry: dict) -> str:
    """`#<pr>  <UTC time>  rc=<n>  <line>`, one landing to a line."""
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(entry["at"]))
    return f"#{entry['pr']}  {stamp}  rc={entry['rc']}  {entry['line']}"
