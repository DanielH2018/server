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
import io
import json
import os
import sys
import tempfile

import pytest

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _HERE)  # block-protected-bash.py imports _hook_common

import _hook_common as hook_common  # noqa: E402


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name.replace("-", "_"), os.path.join(_HERE, f"{name}.py")
    )
    assert spec and spec.loader, "spec_from_file_location found no loader"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_mod = _load("block-protected-bash")

# The arm 1 and 2 tests pass this as `session_cwd`, so arm 3 stays inert for them: it is
# scoped to a session under `.claude/worktrees/`, and this suite runs from one on a deployed
# host, where the repo checkout itself would put every write through the segmenter first.
_ORDINARY = tempfile.gettempdir()


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
    decision, reason = _mod.decide(command, _REPO, session_cwd=_ORDINARY)
    assert decision == "ask", f"should ask: {command}"
    assert reason


@pytest.mark.parametrize("command", CLEAN_WRITES)
def test_an_ordinary_bash_write_is_clean(command):
    decision, _ = _mod.decide(command, _REPO, session_cwd=_ORDINARY)
    assert decision is None, f"should not act on: {command}"


def test_a_write_asks_rather_than_denies():
    """The extraction is a heuristic over command text, so it must never be able to hard-block.

    bash-write-fanout.sh states the bargain a heuristic keeps: a missed detection, never a
    wrong one. `ask` carries the reason the plain permission prompt cannot and leaves the
    decision with the operator; `deny` would turn one bad extraction into unblockable work.
    """
    decision, _ = _mod.decide(
        "sed -i s/a/b/ ansible/vars/secrets.yml", _REPO, session_cwd=_ORDINARY
    )
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
    decision, reason = _mod.decide(command, _REPO, session_cwd=_ORDINARY)
    assert decision == "deny", f"should deny: {command}"
    assert "rotat" in reason.lower()


@pytest.mark.parametrize("command", CLEAN_READS)
def test_a_structural_or_unrelated_read_is_clean(command):
    decision, _ = _mod.decide(command, _REPO, session_cwd=_ORDINARY)
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


def _checkouts(tmp_path):
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


@pytest.fixture
def isolation(tmp_path):
    """`_checkouts`, on a host where arm 3 can run: it splits the command with the dotfiles
    package's segmenter (#2053), which CI does not deploy. There arm 3 answers `ask` for
    every writer-shaped command instead -- `test_a_missing_segmenter_asks...` below covers
    that path, and runs everywhere.
    """
    if hook_common._parse is None:
        pytest.skip(
            "the deployed claude_guard package is not present; arm 3 asks instead"
        )
    return _checkouts(tmp_path)


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


def test_a_redirect_inside_a_heredoc_body_is_clean(isolation):
    """A Markdown blockquote inside a heredoc body is a bare `>`, not a redirect.

    The segmenter lifts the body off the segment text; without that, this command reads as
    writing `{primary}/quote`, and arm 3 would deny a target the command never touches.
    """
    worktree, primary = isolation
    decision, _ = _mod.decide(
        f"cat > notes.md <<'EOF'\n> {primary}/quote\nEOF",
        worktree,
        session_cwd=worktree,
    )
    assert decision is None


def test_the_same_redirect_on_the_heredocs_opening_line_is_flagged(isolation):
    """The near miss: the opening line is not body, so its `>` is a real target."""
    worktree, primary = isolation
    decision, _ = _mod.decide(
        f"cat > {primary}/notes.md <<'EOF'\n> quote\nEOF",
        worktree,
        session_cwd=worktree,
    )
    assert decision == "deny"


# --- #2053: the package segmenter is quote-aware, and the regex it replaced was not ----------


def test_a_cd_inside_quotes_does_not_move_the_carry(isolation):
    """Behaviour change, deliberate. `echo 'x; cd {primary}'` is one word to the shell, so
    the `cd` never runs and the heredoc after `&&` runs in the worktree. The regex segmenter
    split on the quoted `;`, read `cd {primary}'` as a move, and denied a write that never
    left the worktree. `test_a_heredoc_carried_out_of_the_worktree_by_a_cd_is_flagged` is
    the other half of this pair: the same `cd`, unquoted, is still denied."""
    worktree, primary = isolation
    decision, _ = _mod.decide(
        f"echo 'x; cd {primary}' && {HEREDOC}", worktree, session_cwd=worktree
    )
    assert decision is None


def test_an_unreadable_command_asks_rather_than_denies(isolation):
    """A non-ok parse is a refusal, never a skip (the package's contract) -- but the weaker
    refusal: an unbalanced quote is a typo, not evidence of an escape."""
    worktree, primary = isolation
    decision, reason = _mod.decide(
        f"echo 'oops > {primary}/README.md", worktree, session_cwd=worktree
    )
    assert decision == "ask"
    assert "unbalanced-quote" in reason


def test_an_apostrophe_inside_a_heredoc_body_is_clean(isolation):
    """The near miss for the ask above, and this repo's most common command shape: a heredoc
    body is lifted whole, so a quote inside it is prose, not an unbalanced quote."""
    worktree, _ = isolation
    decision, _ = _mod.decide(
        "python3 - <<'PYEOF'\nprint(\"don't\")\nPYEOF", worktree, session_cwd=worktree
    )
    assert decision is None


def test_an_unreadable_command_with_no_writer_is_clean(isolation):
    """Arm 3 only ever acts on a redirect, an in-place editor, `tee` or a heredoc, so text
    carrying none of those cannot be an escape however badly it parses."""
    worktree, _ = isolation
    decision, _ = _mod.decide("echo 'oops", worktree, session_cwd=worktree)
    assert decision is None


def _no_segmenter(command):
    """The half-deployed host's splitter: hook code present, `claude_guard` not yet applied."""
    raise hook_common.Unsplittable(
        "segmenter-missing", "claude_guard package not found"
    )


def test_a_missing_segmenter_asks_for_a_writer_shaped_command(tmp_path):
    """Deny-side: a silent fail-open here would retire the #1419 rule on exactly the host the
    `_claude_guard` DECIDED marker was written about, so the missing package is an `ask`
    that names the fix."""
    worktree, primary = _checkouts(tmp_path)
    decision, reason = _mod.decide(
        f"cd {primary} && {HEREDOC}",
        worktree,
        session_cwd=worktree,
        split=_no_segmenter,
    )
    assert decision == "ask"
    assert "chezmoi apply" in reason


def test_a_missing_segmenter_leaves_a_read_alone(tmp_path):
    worktree, primary = _checkouts(tmp_path)
    decision, _ = _mod.decide(
        f"cd {primary} && grep -rn token .",
        worktree,
        session_cwd=worktree,
        split=_no_segmenter,
    )
    assert decision is None


def test_a_missing_segmenter_is_inert_outside_an_isolated_session(tmp_path):
    _, primary = _checkouts(tmp_path)
    decision, _ = _mod.decide(
        f"cd {primary} && {HEREDOC}", primary, session_cwd=primary, split=_no_segmenter
    )
    assert decision is None


# ── the payload's `cwd`, and what stands in for it ───────────────────────────────────


def _run_main(monkeypatch, capsys, payload):
    monkeypatch.setattr(_mod.sys, "stdin", io.StringIO(json.dumps(payload)))
    assert _mod.main() == 0
    out = capsys.readouterr().out.strip()
    return json.loads(out)["hookSpecificOutput"] if out else None


def test_a_payload_without_cwd_resolves_paths_in_the_process_cwd(monkeypatch, capsys):
    """The `DECIDED` at `repo_root` (#2135): the shim's `cd` makes the process cwd the primary
    checkout, so arm 1 still has a tree to classify a relative path against."""
    monkeypatch.chdir(_REPO)
    out = _run_main(
        monkeypatch,
        capsys,
        {"tool_name": "Bash", "tool_input": {"command": FLAGGED_WRITES[0]}},
    )
    assert out and out["permissionDecision"] == "ask"


def test_a_payload_with_cwd_does_not_read_the_process_cwd(
    monkeypatch, capsys, tmp_path
):
    """The near miss: a `cwd` in the payload wins, and the same relative path names nothing
    protected there, so the process cwd being the repo must not leak into the verdict."""
    monkeypatch.chdir(_REPO)
    out = _run_main(
        monkeypatch,
        capsys,
        {
            "tool_name": "Bash",
            "tool_input": {"command": FLAGGED_WRITES[0]},
            "cwd": str(tmp_path),
        },
    )
    assert out is None
