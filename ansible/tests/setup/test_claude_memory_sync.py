#!/usr/bin/env python3
"""claude-memory-sync copies daniel-box's Claude memory store to daniel-server, one way.

Issue #3123: sessions on each host read that host's own store, and on 2026-10-01 the two
shared 2 file names out of 85 and 66, with daniel-server's index telling sessions to stay on
master. The operator chose daniel-box as the source and an overwriting copy. Three things
decide whether the copy does that rather than something quieter or worse:

1. Direction and `--delete`. Reversed, the copy overwrites the store sessions write; without
   `--delete`, the far store keeps every entry daniel-box archived, which is the defect.
2. The empty-source guard. `--delete` from an empty or missing directory wipes the far store.
3. The arming. True only on daniel-box, and false must remove the units, not leave a timer.

Run: uv run pytest ansible/tests/setup/test_claude_memory_sync.py
"""

import re

from _helpers import ANSIBLE, load_yaml
from lib.repo_paths import HOST_VARS
from _setup_render import render_setup_text, role_context

ROLE = ANSIBLE / "roles" / "setup" / "claude_code"
DEFAULTS = load_yaml(ROLE / "defaults" / "main.yml")
SERVICE = "claude-memory-sync.service.j2"


def _render(**overrides: object) -> str:
    """The sync unit rendered at inventory values, `overrides` on top.

    Through `_setup_render` rather than a context this module assembles: the defaults here are
    themselves Jinja (`claude_code_memory_sync_dir` derives from the checkout path), and the
    harness resolves them the way Ansible does (#3202).
    """
    return render_setup_text("claude_code", SERVICE, overrides)


def _var(name: str, **overrides: object) -> str:
    """One of the role's variables as the render resolves it, `overrides` on top.

    The store path is a derived DEFAULT rather than anything the unit prints on its own line,
    so the derivation is read out of the same resolved context the unit renders with.
    """
    return role_context(ROLE, overrides)[name]


def _exec_start(rendered: str) -> str:
    folded = re.sub(r"\\\n\s*", " ", rendered)
    lines = [ln for ln in folded.splitlines() if ln.startswith("ExecStart=")]
    assert len(lines) == 1, f"expected one ExecStart, got {lines}"
    return " ".join(lines[0].split())


def test_store_path_derives_from_the_checkout():
    """Claude Code keys a store by the working directory with `/` turned into `-`."""
    store = "/home/ubuntu/.claude/projects/-home-ubuntu-server/memory"
    assert _var("claude_code_memory_sync_dir") == store
    moved = _var("claude_code_memory_sync_dir", claude_code_rc_workdir="/srv/x")
    assert moved == "/home/ubuntu/.claude/projects/-srv-x/memory", (
        "the store path must follow claude_code_rc_workdir, not hardcode the checkout"
    )


def test_copy_runs_from_this_host_to_the_target_with_delete():
    store = "/home/ubuntu/.claude/projects/-home-ubuntu-server/memory"
    cmd = _exec_start(_render())
    assert cmd.endswith(f" {store}/ daniel-server:{store}/"), (
        f"the copy must push the local store to the target, trailing slashes on both; got {cmd}"
    )
    assert "--delete" in cmd, (
        "without --delete the far store keeps every entry this host archived"
    )
    assert "BatchMode=yes" in cmd, "a missing key must fail the unit, not prompt"


def test_copy_target_follows_the_variable():
    cmd = _exec_start(_render(claude_code_memory_sync_target="other"))
    assert " other:/home/ubuntu/" in cmd and "daniel-server" not in cmd


def test_an_empty_source_never_reaches_delete():
    rendered = _render()
    store = "/home/ubuntu/.claude/projects/-home-ubuntu-server/memory"
    assert f"ExecCondition=/usr/bin/test -s {store}/MEMORY.md" in rendered, (
        "--delete from an empty or missing store would wipe the far one; the unit must "
        "skip unless the index exists and is non-empty"
    )
    assert "OnFailure=claude-memory-sync-alert.service" in rendered


def test_armed_only_on_daniel_box_and_disarming_removes_the_units():
    assert DEFAULTS["claude_code_memory_sync_enabled"] is False
    box = load_yaml(HOST_VARS / "daniel-box.yml")
    server = load_yaml(HOST_VARS / "daniel-server.yml")
    assert box.get("claude_code_memory_sync_enabled") is True
    assert not server.get("claude_code_memory_sync_enabled", False), (
        "daniel-server must never push: it would overwrite the store sessions write"
    )

    tasks = load_yaml(ROLE / "tasks" / "main.yml")
    removal = [
        t
        for t in tasks
        if t.get("ansible.builtin.file", {}).get("state") == "absent"
        and "claude-memory-sync.timer" in t.get("loop", [])
    ]
    assert removal and removal[0]["when"] == "not claude_code_memory_sync_enabled", (
        "disarming must remove the units, or a stale timer keeps copying"
    )
