#!/usr/bin/env python3
"""Observability-only permission hook: aggregate Claude Code tool-call + prompt
counts into a per-host store so the allowlist can be tuned with data.

Wired (via log-permission.sh) on PreToolUse / PermissionRequest / Notification.
It is `async` and swallows ALL errors — it must never block or break a tool call.
It records *aggregated counts* (keyed by tool + a normalized one-line summary),
never raw event streams: volatile temp paths collapse to "<tmp>" so one-off
commands don't grow cardinality forever, and a retention horizon + hard cap keep
the store bounded.

Companion report/suggester: .claude/scripts/audit-permissions.py (and the
/audit-permissions skill). Pure stdlib, no third-party deps.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone

SCOPE = re.compile(r"^(Bash|Write|Edit|WebFetch|WebSearch|Task|Agent|Skill|mcp__.*)$")
SUM_CAP = 500
SEP = " "
MAX_ENTRIES = 5000
RETENTION_DAYS = 180
ASK_DEDUP_MS = 2000
LOCK_RETRIES = 10
LOCK_DELAY_MS = 20

STORE_PATH = os.environ.get("PERMLOG_STORE") or os.path.normpath(
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "logs", "permissions.json"
    )
)
LOCK_PATH = STORE_PATH + ".lock"


# --- time helpers -----------------------------------------------------------


def parse_ms(iso):
    """Parse an ISO-8601 timestamp to epoch milliseconds. A trailing 'Z' and a
    missing offset are both treated as UTC (matching JS Date.parse semantics)."""
    s = str(iso).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def iso_from_ms(ms):
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + "{:03d}Z".format(dt.microsecond // 1000)


def now_iso():
    return iso_from_ms(int(time.time() * 1000))


# --- classification / summarization -----------------------------------------


def in_scope(tool):
    return isinstance(tool, str) and SCOPE.match(tool) is not None


def cap(s):
    s = "" if s is None else str(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:SUM_CAP] if len(s) > SUM_CAP else s


# Collapse volatile temp-file paths to a stable "<tmp>" token so otherwise-
# identical one-off commands aggregate into one entry instead of bloating
# cardinality forever. Only clearly-temp locations are touched; real project
# paths pass through unchanged. Covers Windows / macOS / Linux so the same store
# format is portable across hosts.
_WIN_TEMP = re.compile(
    r"[A-Za-z]:[\\/]Users[\\/][^\\/\s\"']+[\\/]AppData[\\/]Local[\\/]Temp[\\/][^\s\"')]+",
    re.IGNORECASE,
)
_MAC_TEMP = re.compile(r"/var/folders/[^\s\"')]+")
_NIX_TEMP = re.compile(r"/tmp/[^\s\"')]+")


def normalize_volatile(s):
    s = "" if s is None else str(s)
    s = _WIN_TEMP.sub("<tmp>", s)
    s = _MAC_TEMP.sub("<tmp>", s)
    s = _NIX_TEMP.sub("<tmp>", s)
    return s


def summarize(tool, tool_input):
    tool_input = tool_input or {}
    if tool == "Bash":
        return cap(normalize_volatile(tool_input.get("command")))
    if tool in ("Write", "Edit"):
        return cap(tool_input.get("file_path"))
    if tool == "WebFetch":
        return cap(tool_input.get("url"))
    if tool == "WebSearch":
        return cap(tool_input.get("query"))
    if tool in ("Task", "Agent"):
        parts = [
            p
            for p in (tool_input.get("subagent_type"), tool_input.get("description"))
            if p
        ]
        return cap(": ".join(parts))
    if tool == "Skill":
        return cap(tool_input.get("skill") or tool_input.get("command"))
    if isinstance(tool, str) and tool.startswith("mcp__"):
        keys = list(tool_input.keys())[:3]
        return cap(" ".join(k + "=" + str(tool_input[k]) for k in keys))
    return ""


def key_for(tool, summary):
    return tool + SEP + summary


def classify(d):
    evt = d.get("hook_event_name")
    tool = d.get("tool_name")
    if evt == "PreToolUse":
        if not in_scope(tool):
            return None
        return {
            "kind": "call",
            "tool": tool,
            "sum": summarize(tool, d.get("tool_input")),
        }
    if evt == "PermissionRequest":
        if not in_scope(tool):
            return None
        return {
            "kind": "ask",
            "tool": tool,
            "sum": summarize(tool, d.get("tool_input")),
        }
    if evt == "Notification" and d.get("notification_type") == "permission_prompt":
        # If tool_name is absent, in_scope() is false and the event is dropped.
        # The "(unattributed)" fallback only applies when the tool is known but
        # tool_input is missing.
        if not in_scope(tool):
            return None
        summ = (
            summarize(tool, d.get("tool_input"))
            if d.get("tool_input")
            else "(unattributed)"
        )
        return {"kind": "ask", "tool": tool, "sum": summ}
    return None


def apply_event(store, e, now):
    """Fold one classified event into the store. Returns True iff the store
    changed (so the caller only re-saves on a real mutation)."""
    now_ms = parse_ms(now)
    last_by_tool = store.setdefault("lastAskByTool", {})
    # A single prompt can surface as both a PermissionRequest (carries the command)
    # and a Notification (often without it → "(unattributed)"). Suppress the
    # unattributed echo when any ask for this tool fired within the window so the
    # pair counts once. Checked before touching entries so no phantom
    # "(unattributed)" entry is created. Attributed asks are never per-tool-deduped,
    # so genuinely distinct prompts (e.g. parallel tool calls) are still each counted.
    if e["kind"] == "ask" and e["sum"] == "(unattributed)":
        prev = last_by_tool.get(e["tool"])
        tool_last_ms = parse_ms(prev) if prev else 0
        if now_ms - tool_last_ms < ASK_DEDUP_MS:
            return False
    key = key_for(e["tool"], e["sum"])
    ent = store["entries"].get(key)
    if ent is None:
        ent = store["entries"][key] = {
            "tool": e["tool"],
            "sum": e["sum"],
            "calls": 0,
            "asks": 0,
            "first": now,
            "last": now,
        }
    if e["kind"] == "call":
        ent["calls"] += 1
        ent["last"] = now
        store["updated"] = now
        return True
    last_ask_ms = parse_ms(ent["lastAsk"]) if ent.get("lastAsk") else 0
    if now_ms - last_ask_ms >= ASK_DEDUP_MS:
        ent["asks"] += 1
        ent["lastAsk"] = now
        ent["last"] = now
        last_by_tool[e["tool"]] = now
        store["updated"] = now
        return True
    return False


def prune_if_needed(store, now_ms):
    # Always drop entries past the retention horizon (cheap, keeps one-off noise
    # from lingering for months), then hard-cap the entry count as a backstop.
    cutoff = now_ms - RETENTION_DAYS * 86400000
    for k in list(store["entries"].keys()):
        if parse_ms(store["entries"][k]["last"]) < cutoff:
            del store["entries"][k]
    remaining = list(store["entries"].keys())
    if len(remaining) > MAX_ENTRIES:
        remaining.sort(key=lambda k: parse_ms(store["entries"][k]["last"]))
        for k in remaining[: len(remaining) - MAX_ENTRIES]:
            del store["entries"][k]
    return store


# --- store persistence + locking --------------------------------------------
# fcntl.flock auto-releases on process death, so there is no stale-lock to reap
# (unlike a presence-based lockfile). The store is replaced via atomic rename, so
# the lock is held on a stable sidecar file, not on the store inode itself.


def acquire_lock():
    try:
        import fcntl
    except ImportError:
        return None
    try:
        fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
    except OSError:
        return None
    for _ in range(LOCK_RETRIES):
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError:
            time.sleep(LOCK_DELAY_MS / 1000)
    os.close(fd)
    return None


def release_lock(fd):
    if fd is None:
        return
    try:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        os.close(fd)
    except Exception:
        pass


def load_store():
    try:
        with open(STORE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"version": 1, "updated": None, "entries": {}}


def save_store(store):
    tmp = "{}.tmp.{}.{}".format(STORE_PATH, os.getpid(), int(time.time() * 1000))
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(store, f, indent=2)
        os.replace(tmp, STORE_PATH)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def main():
    try:
        raw = sys.stdin.read()
    except Exception:
        return
    try:
        d = json.loads(raw)
    except Exception:
        return
    e = classify(d)
    if not e:
        return
    try:
        os.makedirs(os.path.dirname(STORE_PATH), exist_ok=True)
    except Exception:
        pass
    fd = acquire_lock()
    if fd is None:
        return
    try:
        now = now_iso()
        store = load_store()
        if "entries" not in store:
            store["entries"] = {}
        if apply_event(store, e, now):
            prune_if_needed(store, int(time.time() * 1000))
            save_store(store)
    except Exception:
        # never disrupt the tool call
        pass
    finally:
        release_lock(fd)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
