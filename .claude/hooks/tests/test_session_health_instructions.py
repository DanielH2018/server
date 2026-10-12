#!/usr/bin/env python3
"""Tests for the instructions-log rows session-health.py writes at SessionStart (#4656).

Claude Code fires no InstructionsLoaded event for the auto-memory `MEMORY.md` or for the
active output style, so log-instructions.py never rows them and `fact_status.py report`
under-counts each session's at-start bytes. The SessionStart hook rows both instead.

Each test runs main() in-process with every probe faked, as `test_session_health.py`'s
`_run_main` does, under a scratch HOME and a scratch instructions log. The primary checkout
is the hook's own answer, `_hook_common.primary_checkout()`, so the test and the hook name
the same memory directory whether the suite runs in a linked worktree or a plain clone.

Run: uv run pytest .claude/hooks/tests/test_session_health_instructions.py
"""

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from lib.git_testing import git, init_repo

_HOOKS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_HOOKS))

from _hook_common import INSTRUCTIONS_LOG_ENV, primary_checkout  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "session_health_instructions", _HOOKS / "session-health.py"
)
assert _spec and _spec.loader, "spec_from_file_location found no loader"
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

_FACT_STATUS = _HOOKS.parents[1] / "scripts" / "dev" / "fact_status.py"
_SESSION = "cccccccc-4656"
_STYLE = "probe-style"
_BYTES = re.compile(r"(?<!\S)bytes=(\d+)(?!\S)")


def _slug(path):
    """Claude Code's project directory name: every non-alphanumeric character becomes `-`."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def _memory_file(home, checkout):
    return home / ".claude" / "projects" / _slug(checkout) / "memory" / "MEMORY.md"


def _session(
    tmp_path, monkeypatch, memory_bytes=4000, style_bytes=1500, source="startup"
):
    """A scratch HOME holding MEMORY.md for the primary checkout and the configured style.

    Returns (home, log, memory, style). The session's cwd is this checkout, which is a linked
    worktree when the suite runs from one. Both files are ASCII, so a size is a byte count.
    """
    home = tmp_path / "home"
    primary = primary_checkout(str(_HOOKS))
    assert primary, "the suite must run inside a git checkout"
    memory = _memory_file(home, os.path.realpath(primary))
    # In a plain clone the worktree test's decoy already holds this directory.
    memory.parent.mkdir(parents=True, exist_ok=True)
    memory.write_bytes(b"- [m](m.md) - pointer\n" + b"m" * (memory_bytes - 22))
    styles = home / ".claude" / "output-styles"
    styles.mkdir(parents=True)
    style = styles / f"{_STYLE}.md"
    head = f"---\nname: {_STYLE}\ndescription: probe\n---\n".encode()
    style.write_bytes(head + b"s" * (style_bytes - len(head)))
    # A second style the settings do not name, so a hook that rows every style is caught.
    (styles / "unused-style.md").write_bytes(
        b"---\nname: unused-style\n---\n" + b"u" * 700
    )
    (home / ".claude" / "settings.json").write_text(json.dumps({"outputStyle": _STYLE}))

    log = tmp_path / "instructions.log"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv(INSTRUCTIONS_LOG_ENV, str(log))
    cwd = str(_HOOKS.parents[1])
    monkeypatch.chdir(cwd)
    payload = {"source": source, "session_id": _SESSION, "cwd": cwd}
    assert (
        _mod.main(
            read_payload=lambda: payload,
            target_problems=lambda: [],
            master_moved_problems=lambda: [],
            parked_deployer_problems=lambda: [],
            other_live_sessions=lambda cwd: [],
            unlabelled_worktree_lines=lambda cwd: [],
            stale_worktree_lines=lambda: [],
            remote_fanout_lines=lambda: [],
        )
        == 0
    )
    return home, log, memory, style


def _start_rows_for(log, target, reason="session_start"):
    """The `bytes=` values of this session's `reason` rows whose path resolves to `target`."""
    text = log.read_text(encoding="utf-8") if log.exists() else ""
    sizes = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 4 or fields[1] != f"[{_SESSION[:8]}]":
            continue
        if fields[2] != reason:
            continue
        # The memory-type column may be blank, so the path is field 3 or 4.
        paths = [f for f in fields[3:] if "=" not in f]
        if any(os.path.realpath(os.path.expanduser(p)) == str(target) for p in paths):
            size = _BYTES.search(line)
            sizes.append(int(size.group(1)) if size else None)
    return sizes


def _report_sessions(tmp_path, log):
    """`fact_status.py report --json`'s sessions over `log`, keyed by the 8-char session id."""
    repo = init_repo(tmp_path / "repo")
    (repo / "CLAUDE.md").write_text("## Style\nwhy, not what.\n")
    (repo / "docs").mkdir()
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "i", "--no-gpg-sign")

    out = subprocess.run(
        [sys.executable, str(_FACT_STATUS), "report", "--repo", str(repo)]
        + ["--log", str(log), "--json"],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    ).stdout
    return {s["session"]: s for s in json.loads(out)["sessions"]}


def test_report_counts_memory_and_output_style_in_the_session_start_total(
    tmp_path, monkeypatch
):
    # Verify-by of #4656: fact_status.py report's at-start total includes both files.
    _home, log, memory, style = _session(tmp_path, monkeypatch)
    sessions = _report_sessions(tmp_path, log)
    expected = memory.stat().st_size + style.stat().st_size
    assert sessions.get(_SESSION[:8], {}).get("start_bytes") == expected, sessions


def test_a_compaction_rows_memory_md_alone_and_the_report_totals_it(
    tmp_path, monkeypatch
):
    # Verify-by of #4668: a compaction reloads MEMORY.md with no InstructionsLoaded event,
    # so the hook rows it under `compact`. The output style is in the system prompt and is
    # not re-injected, so it gets no row, and neither file counts as an at-start load.
    _home, log, memory, style = _session(tmp_path, monkeypatch, source="compact")
    assert _start_rows_for(log, memory.resolve(), "compact") == [memory.stat().st_size]
    assert _start_rows_for(log, style.resolve(), "compact") == []
    session = _report_sessions(tmp_path, log).get(_SESSION[:8], {})
    assert (session.get("start_bytes"), session.get("total_bytes")) == (
        0,
        memory.stat().st_size,
    ), session


def test_session_start_rows_memory_md_with_its_byte_size(tmp_path, monkeypatch):
    _home, log, memory, _style = _session(tmp_path, monkeypatch)
    assert _start_rows_for(log, memory.resolve()) == [memory.stat().st_size]


def test_session_start_rows_the_output_style_the_settings_name(tmp_path, monkeypatch):
    # bytes= is the whole file's size, as log-instructions.py records for a loaded doc.
    _home, log, _memory, style = _session(tmp_path, monkeypatch)
    assert _start_rows_for(log, style.resolve()) == [style.stat().st_size]


def test_a_worktree_session_rows_the_primary_checkouts_memory_md(tmp_path, monkeypatch):
    # A worktree session loads the PRIMARY checkout's MEMORY.md. The decoy sits under the
    # slug of the session's own cwd; it discriminates only when the suite runs from a linked
    # worktree, and in a plain clone it is the same file the primary row names.
    home = tmp_path / "home"
    cwd = os.path.realpath(_HOOKS.parents[1])
    decoy = _memory_file(home, cwd)
    decoy.parent.mkdir(parents=True)
    decoy.write_bytes(b"d" * 2500)
    _home, log, memory, style = _session(tmp_path, monkeypatch)

    sized = (
        [
            int(m.group(1))
            for line in log.read_text(encoding="utf-8").splitlines()
            if f"[{_SESSION[:8]}]" in line and " session_start " in line
            for m in [_BYTES.search(line)]
            if m
        ]
        if log.exists()
        else []
    )
    assert sorted(sized) == sorted([memory.stat().st_size, style.stat().st_size]), sized
