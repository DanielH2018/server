#!/usr/bin/env python3
"""Tests for the log-permission observability hook (Python port).

The hook is observability-only: it must NEVER disrupt a tool call, and it
aggregates permission events into a per-host counts store. These tests lock the
classification, summarization, dedup, and pruning contract, plus the end-to-end
stdin → store behavior (including that malformed input and lock contention are
swallowed silently).

Run: uv run pytest .claude/hooks
(Still importable standalone — it loads the hook by path, no third-party deps.)
"""

import fcntl
import importlib.util
import json
import os
import subprocess
import sys

_HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "log-permission.py")
_spec = importlib.util.spec_from_file_location("log_permission", _HOOK)
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)


# --- pure classification / summarization ------------------------------------


def test_in_scope_matches_bash_and_mcp_rejects_read():
    assert m.in_scope("Bash") is True
    assert m.in_scope("mcp__context7__query-docs") is True
    assert m.in_scope("Skill") is True
    assert m.in_scope("Read") is False
    assert m.in_scope("Glob") is False
    assert m.in_scope(None) is False


def test_cap_collapses_whitespace_trims_and_caps_length():
    assert m.cap("  git   status \n") == "git status"
    assert m.cap(None) == ""
    assert len(m.cap("a" * 600)) == 500


def test_summarize_extracts_the_right_field_per_tool():
    assert m.summarize("Bash", {"command": "git status"}) == "git status"
    assert m.summarize("Write", {"file_path": "/x/y.md"}) == "/x/y.md"
    assert m.summarize("WebFetch", {"url": "https://e.com"}) == "https://e.com"
    assert m.summarize("WebSearch", {"query": "node test"}) == "node test"
    assert (
        m.summarize("Task", {"subagent_type": "Explore", "description": "find x"})
        == "Explore: find x"
    )
    assert m.summarize("Skill", {"skill": "vault-commit"}) == "vault-commit"


def test_summarize_handles_mcp_tools_via_first_keys():
    assert (
        m.summarize("mcp__ctx__query", {"library_id": "react", "tokens": 5000})
        == "library_id=react tokens=5000"
    )


def test_key_for_joins_tool_and_sum_with_space():
    assert m.key_for("Bash", "git status") == "Bash" + m.SEP + "git status"


def test_classify_maps_pretooluse_to_call_out_of_scope_to_none():
    assert m.classify(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
        }
    ) == {"kind": "call", "tool": "Bash", "sum": "ls"}
    assert (
        m.classify(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Read",
                "tool_input": {"file_path": "/x"},
            }
        )
        is None
    )


def test_classify_maps_permissionrequest_and_notification_to_asks():
    assert m.classify(
        {
            "hook_event_name": "PermissionRequest",
            "tool_name": "Bash",
            "tool_input": {"command": "npm i"},
        }
    ) == {"kind": "ask", "tool": "Bash", "sum": "npm i"}
    assert m.classify(
        {
            "hook_event_name": "Notification",
            "notification_type": "permission_prompt",
            "tool_name": "Bash",
        }
    ) == {"kind": "ask", "tool": "Bash", "sum": "(unattributed)"}
    assert (
        m.classify(
            {
                "hook_event_name": "Notification",
                "notification_type": "idle_prompt",
                "tool_name": "Bash",
            }
        )
        is None
    )


# --- temp-path normalization ------------------------------------------------


def test_summarize_collapses_windows_temp_paths():
    a = m.summarize(
        "Bash", {"command": 'node "C:/Users/daniel/AppData/Local/Temp/qedit.js"'}
    )
    b = m.summarize(
        "Bash", {"command": 'node "C:/Users/daniel/AppData/Local/Temp/qedit2.js"'}
    )
    assert a == 'node "<tmp>"'
    assert a == b


def test_summarize_collapses_tmp_and_backslash_temp_paths():
    assert m.summarize("Bash", {"command": "cat /tmp/diag-12345.log"}) == "cat <tmp>"
    assert (
        m.summarize(
            "Bash", {"command": 'node "C:\\Users\\daniel\\AppData\\Local\\Temp\\x.js"'}
        )
        == 'node "<tmp>"'
    )


def test_summarize_collapses_macos_var_folders_temp_paths():
    assert (
        m.summarize("Bash", {"command": "node /var/folders/xy/abc123def/T/qedit.js"})
        == "node <tmp>"
    )


def test_summarize_leaves_non_temp_vault_paths_untouched():
    cmd = 'node "C:/Users/daniel/My_Vault/.claude/scripts/check-links.js"'
    assert m.summarize("Bash", {"command": cmd}) == cmd


# --- applyEvent: counting + dedup -------------------------------------------


def test_apply_event_increments_calls_and_sets_first_last():
    store = {"version": 1, "updated": None, "entries": {}}
    m.apply_event(
        store, {"kind": "call", "tool": "Bash", "sum": "ls"}, "2026-06-17T10:00:00.000Z"
    )
    m.apply_event(
        store, {"kind": "call", "tool": "Bash", "sum": "ls"}, "2026-06-17T10:01:00.000Z"
    )
    e = store["entries"]["Bash" + m.SEP + "ls"]
    assert e["calls"] == 2
    assert e["asks"] == 0
    assert e["first"] == "2026-06-17T10:00:00.000Z"
    assert e["last"] == "2026-06-17T10:01:00.000Z"


def test_apply_event_dedups_asks_within_window_and_reports_change():
    store = {"version": 1, "updated": None, "entries": {}}
    assert (
        m.apply_event(
            store,
            {"kind": "ask", "tool": "Bash", "sum": "npm i"},
            "2026-06-17T10:00:00.000Z",
        )
        is True
    )
    assert (
        m.apply_event(
            store,
            {"kind": "ask", "tool": "Bash", "sum": "npm i"},
            "2026-06-17T10:00:01.000Z",
        )
        is False
    )
    assert (
        m.apply_event(
            store,
            {"kind": "ask", "tool": "Bash", "sum": "npm i"},
            "2026-06-17T10:00:06.000Z",
        )
        is True
    )
    assert store["entries"]["Bash" + m.SEP + "npm i"]["asks"] == 2


def test_apply_event_skips_unattributed_echo_of_recent_attributed():
    store = {"version": 1, "updated": None, "entries": {}}
    assert (
        m.apply_event(
            store,
            {"kind": "ask", "tool": "Bash", "sum": "npm i"},
            "2026-06-17T10:00:00.000Z",
        )
        is True
    )
    assert (
        m.apply_event(
            store,
            {"kind": "ask", "tool": "Bash", "sum": "(unattributed)"},
            "2026-06-17T10:00:00.500Z",
        )
        is False
    )
    assert store["entries"]["Bash" + m.SEP + "npm i"]["asks"] == 1
    assert "Bash" + m.SEP + "(unattributed)" not in store["entries"]


def test_apply_event_still_counts_standalone_unattributed():
    store = {"version": 1, "updated": None, "entries": {}}
    assert (
        m.apply_event(
            store,
            {"kind": "ask", "tool": "Bash", "sum": "(unattributed)"},
            "2026-06-17T10:00:00.000Z",
        )
        is True
    )
    assert store["entries"]["Bash" + m.SEP + "(unattributed)"]["asks"] == 1


def test_apply_event_counts_distinct_attributed_prompts_seconds_apart():
    store = {"version": 1, "updated": None, "entries": {}}
    assert (
        m.apply_event(
            store,
            {"kind": "ask", "tool": "Bash", "sum": "cmd-a"},
            "2026-06-17T10:00:00.000Z",
        )
        is True
    )
    assert (
        m.apply_event(
            store,
            {"kind": "ask", "tool": "Bash", "sum": "cmd-b"},
            "2026-06-17T10:00:01.000Z",
        )
        is True
    )
    assert store["entries"]["Bash" + m.SEP + "cmd-a"]["asks"] == 1
    assert store["entries"]["Bash" + m.SEP + "cmd-b"]["asks"] == 1


# --- pruning ----------------------------------------------------------------


def _ms(iso):
    return m.parse_ms(iso)


def test_prune_if_needed_is_noop_under_cap():
    store = {
        "version": 1,
        "updated": None,
        "entries": {
            "a": {
                "tool": "Bash",
                "sum": "a",
                "calls": 1,
                "asks": 0,
                "first": "x",
                "last": "2026-06-17T10:00:00.000Z",
            }
        },
    }
    m.prune_if_needed(store, _ms("2026-06-18T00:00:00.000Z"))
    assert len(store["entries"]) == 1


def test_prune_if_needed_drops_stale_entries_when_over_cap():
    now = _ms("2026-06-17T00:00:00.000Z")
    store = {"version": 1, "updated": None, "entries": {}}
    store["entries"]["fresh"] = {
        "tool": "Bash",
        "sum": "fresh",
        "calls": 1,
        "asks": 0,
        "first": "x",
        "last": "2026-06-17T00:00:00.000Z",
    }
    stale = m.iso_from_ms(now - 365 * 86400000)
    for i in range(m.MAX_ENTRIES):
        store["entries"]["s" + str(i)] = {
            "tool": "Bash",
            "sum": "s" + str(i),
            "calls": 1,
            "asks": 0,
            "first": "x",
            "last": stale,
        }
    m.prune_if_needed(store, now)
    assert "fresh" in store["entries"]
    assert len(store["entries"]) <= m.MAX_ENTRIES


def test_prune_if_needed_drops_entries_older_than_retention_even_under_cap():
    now = _ms("2026-06-17T00:00:00.000Z")
    old = m.iso_from_ms(now - (m.RETENTION_DAYS + 20) * 86400000)
    store = {
        "version": 1,
        "updated": None,
        "entries": {
            "stale": {
                "tool": "Bash",
                "sum": "stale",
                "calls": 1,
                "asks": 0,
                "first": "x",
                "last": old,
            },
            "fresh": {
                "tool": "Bash",
                "sum": "fresh",
                "calls": 1,
                "asks": 0,
                "first": "x",
                "last": "2026-06-16T00:00:00.000Z",
            },
        },
    }
    m.prune_if_needed(store, now)
    assert "stale" not in store["entries"]
    assert "fresh" in store["entries"]


# --- end-to-end: real subprocess, real store --------------------------------


def _run_hook(store_path, payload):
    env = dict(os.environ, PERMLOG_STORE=store_path)
    subprocess.run(
        [sys.executable, _HOOK],
        input=json.dumps(payload),
        text=True,
        env=env,
        check=True,
    )


def test_e2e_call_then_prompt_yields_calls1_asks1(tmp_path):
    store_path = str(tmp_path / "permissions.json")
    _run_hook(
        store_path,
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "deploy.sh"},
        },
    )
    _run_hook(
        store_path,
        {
            "hook_event_name": "PermissionRequest",
            "tool_name": "Bash",
            "tool_input": {"command": "deploy.sh"},
        },
    )
    store = json.loads(open(store_path, encoding="utf-8").read())
    e = store["entries"]["Bash" + m.SEP + "deploy.sh"]
    assert e["calls"] == 1
    assert e["asks"] == 1


def test_e2e_out_of_scope_and_malformed_input_do_not_write_store(tmp_path):
    store_path = str(tmp_path / "permissions.json")
    _run_hook(
        store_path,
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Read",
            "tool_input": {"file_path": "/x"},
        },
    )
    # malformed stdin must not raise (exit 0); store stays absent
    env = dict(os.environ, PERMLOG_STORE=store_path)
    subprocess.run(
        [sys.executable, _HOOK], input="not json", text=True, env=env, check=True
    )
    assert not os.path.exists(store_path)


def test_e2e_lock_contention_drops_event_without_touching_store(tmp_path):
    store_path = str(tmp_path / "permissions.json")
    lock_path = store_path + ".lock"
    # Actually hold the exclusive flock for the duration of the hook subprocess.
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        _run_hook(
            store_path,
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": "x"},
            },
        )
        assert not os.path.exists(store_path)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
