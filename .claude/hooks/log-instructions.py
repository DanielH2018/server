#!/usr/bin/env python3
# gen-hooks: register
#   event: InstructionsLoaded
#   timeout: 10
#   order: 10
"""Record which CLAUDE.md / .claude/rules file loaded into context, and why.

Usage: Claude Code runs this hook on the InstructionsLoaded event. It reads the hook JSON
payload on stdin, appends one row to `.claude/logs/instructions.log`, prints nothing, and always
exits 0. `-h` or `--help` prints this text and exits 0 without reading stdin.

One line per file as it loads, carrying the file and its `load_reason`: session_start,
path_glob_match, nested_traversal, include, compact. `inject-nested-docs.py` appends a sixth,
`bash_path_match`, through `_hook_common.append_instructions_row`, for a doc it supplied because a Bash command
named a path under it — so one log grades both the harness's loads and the
hook's.

A row this hook writes ends in `bytes=<n>`, the loaded file's size on disk, so
`fact_status.py report` can total the instruction bytes each session loads (#4610). A file it
cannot stat gets no `bytes=` field rather than a wrong one.

The point: verify that path-scoped rules actually fire. Claude Code has known bugs where a
`paths:`-scoped rule isn't loaded when you edit a matching file (or loads globally regardless). With
this log you can edit, say, a compose template and check that `docker.md` shows up with
`load_reason=path_glob_match` and the right trigger.

Observability only — InstructionsLoaded cannot block, and this swallows all errors and always exits
0 so it can never disrupt session startup. Pure stdlib (no third-party deps), like every hook here.

Log: the PRIMARY checkout's .claude/logs/instructions.log (gitignored), even when the hook runs
from a worktree, bounded by single-backup rotation. `_hook_common.instructions_log_path` owns
where it is and why. Inspect: tail -n 40 .claude/logs/instructions.log, run in the primary
checkout.
"""

import os
import sys
from pathlib import Path

from _hook_common import append_instructions_row as append_row
from _hook_common import read_payload


def rel(path, cwd):
    """Make path repo-relative when it's under cwd, else return it unchanged."""
    try:
        if path and cwd and os.path.commonpath([path, cwd]) == cwd:
            return os.path.relpath(path, cwd)
    except Exception:  # noqa: S110 -- an unrelatable path is logged as given
        pass
    return path


def size_field(path, cwd):
    """The ` bytes=<n>` field for the file at `path`, or "" when it cannot be read.

    `path` is the payload's own path, taken before `rel` shortens it: a repo-relative path
    would resolve against this process's cwd, not the session's.
    """
    try:
        return " bytes={}".format((Path(cwd or ".") / path).stat().st_size)
    except OSError, TypeError:
        return ""


def main():
    """Append one line to the instructions log for an InstructionsLoaded event on stdin.

    Ignores any other hook event. The caller wraps this in a broad except so a failure here
    never blocks session startup.
    """
    d = read_payload()
    if d is None or d.get("hook_event_name") != "InstructionsLoaded":
        return
    cwd = d.get("cwd") or ""
    raw = d.get("file_path")
    fp = rel(raw or "?", cwd)
    reason = d.get("load_reason") or "?"
    mtype = d.get("memory_type") or "?"
    sid = (d.get("session_id") or "")[:8]
    extra = ""
    if d.get("trigger_file_path"):
        extra += " trigger=" + rel(d["trigger_file_path"], cwd)
    if d.get("globs"):
        extra += " globs=" + ",".join(d["globs"])
    if d.get("parent_file_path"):
        extra += " parent=" + rel(d["parent_file_path"], cwd)
    # A subagent shares its parent's session id; inject-nested-docs tells the two apart by this.
    if d.get("agent_id"):
        extra += " agent=" + d["agent_id"]
    if raw:
        extra += size_field(raw, cwd)
    append_row(reason, mtype, fp, sid, extra)


if __name__ == "__main__":
    if any(arg in ("-h", "--help") for arg in sys.argv[1:]):
        print(__doc__.strip())
        sys.exit(0)
    try:
        main()
    except Exception:  # noqa: S110 -- a logging hook must never block the tool call
        pass
