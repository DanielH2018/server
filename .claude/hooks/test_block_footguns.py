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


# --- 5. a load generator aimed at the public hostname ------------------------------------------


def test_a_burst_against_the_public_name_is_denied():
    assert (
        _mod.problem("ab -n 120 -c 12 https://n8n.daniel-hunter.com/webhook/burst")
        is not None
    )


def test_the_fix_names_the_local_hostname():
    """Assert on the whole rewritten URL, not a substring of it.

    A substring test would pass on a rewrite that mangled the path, and CodeQL flags the shape
    as incomplete URL sanitization (py/incomplete-url-substring-sanitization) — correctly, as
    the bypass test below shows.
    """
    found = _mod.problem("hey -n 120 https://n8n.daniel-hunter.com/webhook/burst")
    assert found.endswith("https://n8n.local.daniel-hunter.com/webhook/burst")


def test_a_local_segment_in_the_path_does_not_exempt_a_public_host():
    """The bypass in this rule's first draft.

    It tested `".local." not in word` against the WHOLE argument, so a `.local.` anywhere in the
    path read as a LAN target and the burst went through to the public name. Matching on the
    parsed hostname by suffix is what closes it.
    """
    assert (
        _mod.problem("ab -n 120 https://n8n.daniel-hunter.com/x/.local./y") is not None
    )


def test_a_public_host_as_a_query_value_does_not_trip_a_local_target():
    """The mirror: the host decides, so the public name inside a LAN URL's query is not a hit."""
    assert (
        _mod.problem(
            "ab -n 10 https://n8n.local.daniel-hunter.com/?next=n8n.daniel-hunter.com"
        )
        is None
    )


def test_a_bare_host_argument_is_matched():
    """Not every load generator takes a URL; `siege n8n.daniel-hunter.com` is the bare form."""
    assert _mod.problem("siege -c 20 n8n.daniel-hunter.com") is not None


def test_a_lookalike_domain_is_not_matched():
    """Suffix matching, so a domain merely ENDING in the same letters must not be caught."""
    assert _mod.problem("ab -n 100 https://notdaniel-hunter.com/") is None


def test_every_named_burst_tool_is_matched():
    """One rule, ten binaries — a typo in the set would silently exempt that tool."""
    for tool in _mod._BURST_TOOLS:
        assert _mod.problem(f"{tool} https://n8n.daniel-hunter.com/") is not None, tool


def test_a_burst_against_the_local_name_is_clean():
    """The prescribed target. The rule exists to send you here, so it must not fire on it."""
    assert (
        _mod.problem("ab -n 120 -c 12 https://n8n.local.daniel-hunter.com/webhook")
        is None
    )


def test_a_plain_curl_to_the_public_name_is_clean():
    """One request is ordinary. Only a tool built to make many is the signature."""
    assert _mod.problem("curl -sS https://n8n.daniel-hunter.com/healthz") is None


def test_a_burst_against_an_unrelated_host_is_clean():
    assert _mod.problem("ab -n 100 https://example.com/") is None


# --- 6. pgrep -f matching the shell that runs it -----------------------------------------------


def test_a_pgrep_dash_f_waiter_loop_is_denied():
    """The 2026-08-17 shape. shlex leaves `;` attached, so the whole loop is ONE stage."""
    assert (
        _mod.problem("until ! pgrep -f b2_wipe_prefixes; do sleep 15; done") is not None
    )


def test_a_bare_pgrep_dash_f_is_denied():
    assert _mod.problem("pgrep -f b2_wipe_prefixes") is not None


def test_a_bundled_pgrep_flag_is_denied():
    """`-cf` is `-c` and `-f`; the memory records `pgrep -c` carrying the same flaw."""
    assert _mod.problem("pgrep -cf b2_wipe_prefixes") is not None


def test_a_character_class_pattern_is_clean():
    """The documented fix. Its presence is the signal the author knows about the self-match."""
    assert _mod.problem("pgrep -f 'b2_[w]ipe_prefixes'") is None


def test_pgrep_without_dash_f_is_clean():
    """Without -f, pgrep matches process NAMES, so a shell running the string is not a match."""
    assert _mod.problem("pgrep sshd") is None


def test_a_pid_wait_loop_is_clean():
    """The prescribed alternative must not itself trip the rule."""
    assert _mod.problem("while kill -0 12345 2>/dev/null; do sleep 1; done") is None


def test_the_word_pgrep_as_a_grep_argument_is_clean():
    """`pgrep` appearing as data, not as the command being run."""
    assert _mod.problem("grep -rn pgrep .claude/hooks") is None


# --- the shared keyword stripper ---------------------------------------------------------------


def test_leading_keywords_are_stripped_but_later_ones_are_not():
    from _hook_common import strip_shell_keywords

    stage = ["until", "!", "pgrep", "-f", "x;", "do", "sleep", "15;", "done"]
    assert strip_shell_keywords(stage) == [
        "pgrep",
        "-f",
        "x;",
        "do",
        "sleep",
        "15;",
        "done",
    ]


def test_stripping_a_stage_that_is_all_keywords_is_empty_not_an_error():
    from _hook_common import strip_shell_keywords

    assert strip_shell_keywords(["until", "!"]) == []
