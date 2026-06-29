#!/usr/bin/env python3
"""InstructionsLoaded hook (observability only): append one line per CLAUDE.md /
.claude/rules file as it loads into context, recording WHICH file loaded and WHY
(`load_reason`): session_start, path_glob_match, nested_traversal, include, compact.

The point: verify that path-scoped rules actually fire. Claude Code has known bugs
where a `paths:`-scoped rule isn't loaded when you edit a matching file (or loads
globally regardless). With this log you can edit, say, a compose template and check
that `docker.md` shows up with `load_reason=path_glob_match` and the right trigger.

Observability only — InstructionsLoaded cannot block, and this swallows all errors and
always exits 0 so it can never disrupt session startup. Pure stdlib (no third-party
deps), so the wrapper runs system python3 directly — no uv overhead on the startup path.

Log: .claude/logs/instructions.log (gitignored), bounded by single-backup rotation.
Inspect: tail -n 40 .claude/logs/instructions.log
"""
import json
import os
import sys
from datetime import datetime, timezone

LOG = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs", "instructions.log"))
MAX_BYTES = 256 * 1024


def rel(path, cwd):
    """Make path repo-relative when it's under cwd, else return it unchanged."""
    try:
        if path and cwd and os.path.commonpath([path, cwd]) == cwd:
            return os.path.relpath(path, cwd)
    except Exception:
        pass
    return path


def main():
    d = json.loads(sys.stdin.read())
    if d.get("hook_event_name") != "InstructionsLoaded":
        return
    cwd = d.get("cwd") or ""
    fp = rel(d.get("file_path") or "?", cwd)
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
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = "{} [{:8}] {:16} {:8} {}{}\n".format(ts, sid, reason, mtype, fp, extra)

    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    try:                                            # rotate (single backup) when large
        if os.path.getsize(LOG) > MAX_BYTES:
            os.replace(LOG, LOG + ".1")
    except OSError:
        pass
    # A single O_APPEND write below PIPE_BUF (4 KiB) is atomic across processes on
    # POSIX, so concurrent sessions can't interleave a line — no lock needed.
    fd = os.open(LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line.encode("utf-8", "replace"))
    finally:
        os.close(fd)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
