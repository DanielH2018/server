#!/usr/bin/env python3
"""Tests for the four-footgun PreToolUse guard.

Each rule is an accept/reject pair: the command it must refuse, and the near-miss it must let
through. A guard that fires on everything and one that fires on nothing are indistinguishable
from the passing side alone, and three of these four rules key on a single flag or word.

Run: uv run pytest .claude/hooks
"""

import importlib.util
import io
import json
import os
import sys

_HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "block-footguns.py")
sys.path.insert(0, os.path.dirname(_HOOK))
_spec = importlib.util.spec_from_file_location("block_footguns", _HOOK)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


# --- 1. ugrep's -Z and -z ---------------------------------------------------------------------


def test_grep_dash_Z_is_denied():
    assert "--fuzzy" in _mod.problem("grep -Z foo .")


def test_a_bundled_dash_Z_is_denied():
    """The incident was written `grep -lZ ... | xargs -0`, so bundling must be unbundled."""
    assert _mod.problem("grep -rlZ foo . | xargs -0 sed -i s/a/b/")


def test_grep_dash_z_is_denied():
    assert "--decompress" in _mod.problem("grep -z foo .")


def test_grep_with_null_is_clean():
    assert _mod.problem("grep -rl --null foo . | xargs -0 ls") is None


def test_an_ordinary_grep_is_clean():
    assert _mod.problem("grep -rn foo .") is None


def test_a_dash_Z_on_another_binary_is_clean():
    """`-Z` means something else again elsewhere; this rule is about grep."""
    assert _mod.problem("tar -Z -cf out.tar dir") is None


# --- 2. a bare git stash pop --------------------------------------------------------------------


def test_bare_stash_pop_is_denied():
    assert "per-repository" in _mod.problem("git stash pop")


def test_bare_stash_apply_is_denied():
    assert _mod.problem("git stash apply")


def test_stash_pop_with_an_explicit_ref_is_clean():
    assert _mod.problem("git stash pop 'stash@{2}'") is None


def test_git_stash_push_is_clean():
    """Pushing is safe — it adds to the shared stack rather than taking from it."""
    assert _mod.problem("git stash push -m wip") is None


def test_git_stash_list_is_clean():
    assert _mod.problem("git stash list") is None


# --- 3. kubectl rollout restart ------------------------------------------------------------------


def test_rollout_restart_is_denied():
    assert "read-only ServiceAccount" in _mod.problem(
        "kubectl rollout restart deployment/sonarr -n homelab"
    )


def test_rollout_status_is_clean():
    """`rollout status` is a read and works fine as the read-only SA."""
    assert _mod.problem("kubectl rollout status deployment/sonarr -n homelab") is None


def test_kubectl_get_is_clean():
    assert _mod.problem("kubectl get pods -n homelab") is None


# --- 4. remote git with no cd ---------------------------------------------------------------------


def test_remote_git_without_cd_is_denied():
    assert "$HOME" in _mod.problem("ssh daniel-server 'git status'")


def test_remote_git_with_cd_is_clean():
    assert (
        _mod.problem("ssh daniel-server 'cd /home/ubuntu/server; git status'") is None
    )


def test_remote_git_with_dash_C_is_clean():
    assert _mod.problem("ssh daniel-server 'git -C /home/ubuntu/server status'") is None


def test_a_remote_non_git_command_is_clean():
    assert _mod.problem("ssh daniel-pi 'docker ps'") is None


def test_a_local_git_command_is_clean():
    assert _mod.problem("git status") is None


# --- the decision it emits -------------------------------------------------------------------


def _run(monkeypatch, capsys, command):
    payload = {"session_id": "s", "tool_input": {"command": command}}
    monkeypatch.setattr(_mod.sys, "stdin", io.StringIO(json.dumps(payload)))
    assert _mod.main() == 0
    out = capsys.readouterr().out.strip()
    return json.loads(out) if out else None


def test_main_denies_with_the_fix(monkeypatch, capsys):
    decision = _run(monkeypatch, capsys, "git stash pop")
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "stash@" in decision["hookSpecificOutput"]["permissionDecisionReason"]


def test_main_is_silent_on_an_ordinary_command(monkeypatch, capsys):
    assert _run(monkeypatch, capsys, "ls -la") is None


def test_malformed_payload_is_ignored(monkeypatch, capsys):
    monkeypatch.setattr(_mod.sys, "stdin", io.StringIO("{nope"))
    assert _mod.main() == 0
    assert capsys.readouterr().out == ""


def test_a_later_pipeline_stage_is_still_judged():
    assert _mod.problem("git fetch && git stash pop") is not None
