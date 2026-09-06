#!/usr/bin/env python3
"""Tests for the block-protected-bash PreToolUse guard.

Every rule is a `..._is_flagged` / `..._is_clean` pair. A guard that fires on everything and
one that fires on nothing are indistinguishable from the passing side alone, so each arm here
carries the input it must act on AND the near miss it must leave alone.

The four flagged write cases are the exact commands measured on 2026-08-29 against both
existing PreToolUse Bash hooks, each of which returned no decision.

Run: uv run pytest .claude/hooks/tests/test_block_protected_bash.py
"""

import importlib.util
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _HERE)  # block-protected-bash.py imports _hook_common


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name.replace("-", "_"), os.path.join(_HERE, f"{name}.py")
    )
    assert spec and spec.loader, "spec_from_file_location found no loader"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_mod = _load("block-protected-bash")


# ── arm 1: writes to a protected file ────────────────────────────────────────────────

# Paths that exist in this repo and that classify() rejects. secrets.yml is detected by
# CONTENT (its SOPS integrity MAC), docs/reference/ by the generated_from: banner.
FLAGGED_WRITES = [
    "sed -i s/a/b/ ansible/vars/secrets.yml",
    "cat > containers/glances/docker-compose.yml <<EOF",
    "tee docs/reference/services.md",
    "echo x >> ansible/vars/secrets.yml",
    "perl -pi -e s/a/b/ ansible/vars/secrets.yml",
]

# Near misses. Each is one token from a case above and must keep working: host_vars and
# group_vars carry the substring "vars/" but are PLAINTEXT, docs/reference/topology.md is the
# one hand-written page in a generated tree, and a read is not a write.
CLEAN_WRITES = [
    "echo x > /tmp/scratch.txt",
    "sed -i s/a/b/ README.md",
    "cat ansible/vars/secrets.yml",
    "echo x >> ansible/inventory/host_vars/daniel-box.yml",
    "echo x >> docs/reference/topology.md",
    "grep -n 'sed -i ansible/vars/secrets.yml' notes.md",
]


@pytest.mark.parametrize("command", FLAGGED_WRITES)
def test_a_bash_write_to_a_protected_file_is_flagged(command):
    decision, reason = _mod.decide(command, _REPO)
    assert decision == "ask", f"should ask: {command}"
    assert reason


@pytest.mark.parametrize("command", CLEAN_WRITES)
def test_an_ordinary_bash_write_is_clean(command):
    decision, _ = _mod.decide(command, _REPO)
    assert decision is None, f"should not act on: {command}"


def test_a_write_asks_rather_than_denies():
    """The extraction is a heuristic over command text, so it must never be able to hard-block.

    bash-write-fanout.sh states the bargain a heuristic keeps: a missed detection, never a
    wrong one. `ask` carries the reason the plain permission prompt cannot and leaves the
    decision with the operator; `deny` would turn one bad extraction into unblockable work.
    """
    decision, _ = _mod.decide("sed -i s/a/b/ ansible/vars/secrets.yml", _REPO)
    assert decision == "ask"


# ── arm 2: reads that print a secret-bearing host script ─────────────────────────────

# secret-rotation-audit.sh is the file from the 2026-08-28 incident (PR #550): a
# `grep -nE "rotate|--commit|sops set|push"` on it printed a live push token.
INCIDENT = "/usr/local/bin/secret-rotation-audit.sh"

FLAGGED_READS = [
    f'grep -nE "rotate|--commit|sops set|push" {INCIDENT}',
    f"cat {INCIDENT}",
    f"head -20 {INCIDENT}",
    f"sudo cat {INCIDENT}",
    "tail -5 /usr/local/bin/ups-secondary-health.sh",
]

# The safe forms the memory entry prescribes, plus the near misses. `-o` is load-bearing:
# without it grep prints the whole matching line, key and value both.
CLEAN_READS = [
    f"grep -oE '^[A-Z_]+=' {INCIDENT}",
    f"grep -c push {INCIDENT}",
    f"grep -l push {INCIDENT}",
    f"ls -l {INCIDENT}",
    f"shellcheck {INCIDENT}",
    # A host script that embeds no tracked secret. `domain` carries tier: ignore in the
    # registry, so a script mentioning only that is not in the derived set.
    "cat /usr/local/bin/disk-health.sh",
    # Text naming the command is not the command.
    f'echo "cat {INCIDENT}"',
]


@pytest.mark.parametrize("command", FLAGGED_READS)
def test_printing_a_secret_bearing_host_script_is_flagged(command):
    decision, reason = _mod.decide(command, _REPO)
    assert decision == "deny", f"should deny: {command}"
    assert "rotat" in reason.lower()


@pytest.mark.parametrize("command", CLEAN_READS)
def test_a_structural_or_unrelated_read_is_clean(command):
    decision, _ = _mod.decide(command, _REPO)
    assert decision is None, f"should not act on: {command}"


def test_the_cheap_gate_skips_the_derivation_entirely():
    """The derivation walks every role task file (~0.9s) and must not run per Bash call.

    Every path it can return starts with a host bin prefix, so a command naming neither cannot
    match — and this asserts the walk is not reached, not merely that the answer is None.
    """
    called = []
    original = _mod._secret_bearing_paths
    _mod._secret_bearing_paths = lambda root: called.append(root) or {}
    try:
        assert _mod.read_reason("grep -rn token ansible/roles/", _REPO) is None
    finally:
        _mod._secret_bearing_paths = original
    assert called == [], "the derivation ran for a command naming no host bin path"


def test_the_derivation_still_finds_the_incident_file():
    """The rejecting half of the derivation itself: an empty set would make arm 2 inert."""
    paths = _mod._secret_bearing_paths(_REPO)
    assert INCIDENT in paths
    assert "secret_rotation_push_token" in paths[INCIDENT]


# ── arm 3: a write that escapes an isolated session's worktree ───────────────────────
#
# The pairs below are built on a tmp_path fixture rather than on this host's real layout, so
# they mean the same thing in CI — where the checkout is NOT under `.claude/worktrees/` — as
# they do in a worktree session. `_REPO` above resolves to the worktree root when the suite
# runs from one, which is exactly why arm 3's scoping is never derived from it.

HEREDOC = "python3 - <<'EOF'\nopen('x','w').write('y')\nEOF"


@pytest.fixture
def isolation(tmp_path):
    """A fake primary checkout with a worktree under it, both real git checkouts on disk.

    Returns (worktree, primary). `primary/.git` is a directory and `worktree/.git` a file,
    the two shapes git itself uses, so `_inside_a_git_checkout` is exercised on both.
    """
    primary = tmp_path / "server"
    (primary / ".git").mkdir(parents=True)
    worktree = primary / ".claude" / "worktrees" / "agent-1"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {primary}/.git/worktrees/agent-1\n")
    return str(worktree), str(primary)


def test_a_heredoc_carried_out_of_the_worktree_by_a_cd_is_flagged(isolation):
    """The measured 2026-09-06 escape: the `cd` is the whole difference, and nothing saw it."""
    worktree, primary = isolation
    decision, reason = _mod.decide(
        f"cd {primary} && {HEREDOC}", worktree, session_cwd=worktree
    )
    assert decision == "deny"
    assert "#1419" in reason


def test_the_same_heredoc_with_no_cd_is_clean(isolation):
    worktree, _ = isolation
    decision, _ = _mod.decide(HEREDOC, worktree, session_cwd=worktree)
    assert decision is None


def test_a_heredoc_after_a_cd_into_the_sessions_own_worktree_is_clean(isolation):
    worktree, _ = isolation
    decision, _ = _mod.decide(
        f"cd {worktree}/scripts && {HEREDOC}", worktree, session_cwd=worktree
    )
    assert decision is None


# The writer set issue #1419 names: heredoc redirect, `sed -i`, `tee`, `>`, `>>`.
ESCAPING_WRITES = [
    "cd {primary} && cat > notes.md <<'EOF'\nhi\nEOF",
    "cd {primary} && sed -i s/a/b/ README.md",
    "echo x | tee {primary}/README.md",
    "echo x > {primary}/README.md",
    "echo x >> {primary}/README.md",
]

# Near misses, each one token from a case above. A `cd` outside is not itself a write, and a
# writer under a `cd` outside that names a scratch target is judged on the target.
CONTAINED_WRITES = [
    "echo x > README.md",
    "echo x > {worktree}/README.md",
    "sed -i s/a/b/ {worktree}/README.md",
    "cd {primary} && grep -rn token .",
    "cd {primary} && cat README.md > /tmp/copy.txt",
    "cd {primary}",
]


@pytest.mark.parametrize("template", ESCAPING_WRITES)
def test_a_write_escaping_the_worktree_is_flagged(isolation, template):
    worktree, primary = isolation
    command = template.format(primary=primary, worktree=worktree)
    decision, reason = _mod.decide(command, worktree, session_cwd=worktree)
    assert decision == "deny", f"should deny: {command}"
    assert reason


@pytest.mark.parametrize("template", CONTAINED_WRITES)
def test_a_write_that_stays_inside_the_worktree_is_clean(isolation, template):
    worktree, primary = isolation
    command = template.format(primary=primary, worktree=worktree)
    decision, _ = _mod.decide(command, worktree, session_cwd=worktree)
    assert decision is None, f"should not act on: {command}"


def test_arm_three_is_inert_outside_an_isolated_session(isolation):
    """The same escaping command from a session that is not worktree-isolated.

    Without this, arm 3 would deny every ordinary session's writes to its own checkout — and
    the pairs above would pass either way, because `_REPO` is isolated only when the suite
    happens to run from a worktree.
    """
    _, primary = isolation
    decision, _ = _mod.decide(
        f"cd {primary} && {HEREDOC}", primary, session_cwd=primary
    )
    assert decision is None


def test_arm_three_denies_where_arm_one_would_only_ask(isolation):
    """Ordering. A command matching both arms must take the stronger decision."""
    worktree, primary = isolation
    protected = os.path.join("ansible", "vars", "secrets.yml")
    decision, reason = _mod.decide(
        f"cd {primary} && sed -i s/a/b/ {protected}", worktree, session_cwd=worktree
    )
    assert decision == "deny"
    assert "#1419" in reason


def test_the_interpreter_set_contains_the_incidents_own_command_word():
    """Non-vacuity. `_HEREDOC_INTERPRETERS` is the only thing standing between a target-less
    heredoc and a silent escape; a rename emptying it would leave every pair above green."""
    for word in ("python3", "bash", "uv"):
        assert word in _mod._HEREDOC_INTERPRETERS


def test_a_heredoc_body_is_not_scanned_for_redirects():
    """A Markdown blockquote inside a heredoc body is a bare `>`, not a redirect.

    Without the strip, this command reads as writing a file named `quote`, and arm 3 would
    judge a target the command never touches.
    """
    stripped = _mod.strip_heredoc_bodies("cat > notes.md <<'EOF'\n> quote\nEOF")
    assert "quote" not in stripped
    assert "notes.md" in stripped
