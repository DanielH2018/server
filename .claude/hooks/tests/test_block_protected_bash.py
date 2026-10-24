#!/usr/bin/env python3
"""Tests for the block-protected-bash PreToolUse guard.

Every rule is a `..._is_flagged` / `..._is_clean` pair. A guard that fires on everything and
one that fires on nothing are indistinguishable from the passing side alone, so each arm here
carries the input it must act on AND the near miss it must leave alone.

The flagged write cases are the shapes measured on 2026-08-29 against both existing PreToolUse
Bash hooks, each of which returned no decision: an in-place `sed`, a heredoc redirect and a
`tee`.

Run: uv run pytest .claude/hooks/tests/test_block_protected_bash.py
"""

import importlib.util
import io
import json
import os
import subprocess
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
    "cat > docs/assets/generated/infra-map.svg <<EOF",
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


def test_a_bash_write_to_a_sops_dotenv_is_flagged(tmp_path):
    """The cluster file is a SOPS dotenv, and the Bash arm loads the Edit hook's detector."""
    enc = "ENC[" + "AES256_GCM,data:Zz==,iv:Yy=,tag:Xx==,type:str]"
    target = tmp_path / "cluster.env"
    target.write_text("a_key=" + enc + "\n" + "sops_mac=" + enc + "\n")
    decision, reason = _mod.decide(f"sed -i s/a/b/ {target}", str(tmp_path))
    assert decision == "ask"
    assert reason


def test_a_write_into_another_checkouts_generated_tree_is_flagged(tmp_path):
    """The checkout that owns the target decides, not the cwd, as in the Edit hook.

    The generated-docs trees are anchored at the root `classify` is given. A worktree session
    writing by absolute path into the primary checkout, or the reverse, was checked against the
    cwd's trees, which the target is not in, and passed silently.
    """
    (tmp_path / ".git").mkdir()
    decision, reason = _mod.decide(
        f"tee {_REPO}/docs/reference/services.md", str(tmp_path)
    )
    assert decision == "ask"
    assert "docs/reference/services.md" in reason


def test_a_relative_write_from_a_subdirectory_into_a_generated_tree_is_flagged():
    """The same root rule, reached from inside the owning checkout rather than another one."""
    decision, reason = _mod.decide(
        "tee ../docs/reference/services.md", os.path.join(_REPO, "ansible")
    )
    assert decision == "ask"
    assert "docs/reference/services.md" in reason


def test_a_write_into_another_checkouts_hand_written_page_is_clean(tmp_path):
    """The near miss: the owning checkout's banner check still exempts topology.md."""
    (tmp_path / ".git").mkdir()
    decision, _ = _mod.decide(
        f"echo x >> {_REPO}/docs/reference/topology.md", str(tmp_path)
    )
    assert decision is None


def test_a_write_asks_rather_than_denies():
    """The extraction is a heuristic over command text, so it must never be able to hard-block.

    bash-write-fanout.sh states the bargain a heuristic keeps: a missed detection, never a
    wrong one. `ask` carries the reason the plain permission prompt cannot and leaves the
    decision with the operator; `deny` would turn one bad extraction into unblockable work.
    """
    decision, _ = _mod.decide("sed -i s/a/b/ ansible/vars/secrets.yml", _REPO)
    assert decision == "ask"


# ── arm 2: reads that print a secret-bearing host script ─────────────────────────────

# secret-rotation-audit.sh embeds a live push token: a
# `grep -nE "rotate|--commit|sops set|push"` on it prints the token.
INCIDENT = "/usr/local/bin/secret-rotation-audit.sh"

FLAGGED_READS = [
    f'grep -nE "rotate|--commit|sops set|push" {INCIDENT}',
    f"cat {INCIDENT}",
    f"head -20 {INCIDENT}",
    f"sudo cat {INCIDENT}",
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


def test_a_set_held_beside_the_hook_wins_over_the_trees_derivation(tmp_path):
    """A fan-out pipeline ships the set it derived at start; the agent's tree is not read."""
    held = tmp_path / "secret_bearing_host_paths.json"
    held.write_text('{"/usr/local/bin/held.sh": ["held_token"]}')
    assert _mod._secret_bearing_paths(_REPO, held=str(held)) == {
        "/usr/local/bin/held.sh": ["held_token"]
    }
    assert INCIDENT in _mod._secret_bearing_paths(_REPO, held=str(tmp_path / "none"))


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


# ── which checkout's derivation runs, whatever the payload's `cwd` (#4431) ───────────
#
# These run the hook as its own process because the import under test is cached: once any
# test in this process has imported `secret_bearing_host_paths`, a wrong `sys.path` entry
# still finds it in `sys.modules` and the defect cannot show.

_DECOY_MODULE = """
import pathlib

DECOY = "/usr/local/bin/decoy-from-the-second-checkout.sh"


def secret_bearing_host_paths(*args, **kwargs):
    pathlib.Path(__file__).with_name("imported").write_text("yes")
    return {DECOY: ["decoy_token"]}
"""


def _second_checkout(tmp_path):
    """A checkout that is not the hook's, and the file its own module writes on import.

    It has its own `.git`, the real Ansible tree as data, and a `secret_bearing_host_paths`
    that records being imported and derives only a decoy path.
    """
    root = tmp_path / "second-checkout"
    secrets_mgmt = root / "scripts" / "secrets_mgmt"
    secrets_mgmt.mkdir(parents=True)
    (secrets_mgmt / "secret_bearing_host_paths.py").write_text(_DECOY_MODULE)
    (root / ".git").write_text("gitdir: /nonexistent\n")
    (root / "ansible").symlink_to(os.path.join(_REPO, "ansible"))
    (root / "docs").mkdir()
    return root, secrets_mgmt / "imported"


def _run_hook_process(command, cwd):
    """The hook's decision for a Bash payload carrying `cwd`, from a fresh interpreter."""
    assert not os.path.exists(_mod._HELD_PATHS), (
        "a held set beside the hook bypasses the derivation these tests exercise"
    )
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    proc = subprocess.run(
        [sys.executable, os.path.join(_HERE, "block-protected-bash.py")],
        input=json.dumps(
            {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)}
        ),
        capture_output=True,
        text=True,
        cwd=_REPO,
        env=env,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout.strip()
    return json.loads(out)["hookSpecificOutput"] if out else None


def test_a_payload_cwd_in_a_second_checkout_runs_the_hooks_own_derivation(tmp_path):
    """The second checkout's module must not run, and the hook's own must derive a set that
    still holds the incident file."""
    root, imported = _second_checkout(tmp_path)
    out = _run_hook_process(f"cat {INCIDENT}", root)
    assert not imported.exists(), "the hook imported the second checkout's module"
    assert out and out["permissionDecision"] == "deny", out
    assert INCIDENT in out["permissionDecisionReason"]


def test_a_payload_cwd_in_a_second_checkouts_subdirectory_still_derives_a_set(tmp_path):
    """From a subdirectory `<cwd>/scripts/secrets_mgmt` does not exist, and the guard must not
    fall back to an empty set that checks nothing."""
    root, imported = _second_checkout(tmp_path)
    out = _run_hook_process(f"cat {INCIDENT}", root / "docs")
    assert not imported.exists(), "the hook imported the second checkout's module"
    assert out and out["permissionDecision"] == "deny", out
    assert INCIDENT in out["permissionDecisionReason"]


def test_a_payload_cwd_in_another_checkout_derives_from_that_checkouts_tree(tmp_path):
    """The cwd's checkout is data: its Ansible tree and registry decide the set, while the
    code that walks them comes from the hook's checkout. This checkout carries no
    `scripts/` at all, so only the hook's own module can derive anything here."""
    root = tmp_path / "data-only-checkout"
    role_dir = root / "ansible" / "roles" / "demo"
    (role_dir / "tasks").mkdir(parents=True)
    (role_dir / "templates").mkdir()
    (role_dir / "templates" / "thing.sh.j2").write_text("TOKEN={{ demo_push_token }}\n")
    dest = "/usr/local/bin/only-in-the-data-checkout.sh"
    (role_dir / "tasks" / "main.yml").write_text(
        "- name: Install thing\n"
        "  ansible.builtin.template:\n"
        "    src: thing.sh.j2\n"
        f"    dest: {dest}\n"
    )
    (root / "ansible" / "secret_rotation.yml").write_text(
        "entries:\n  demo_push_token:\n    tier: auto\n    last_rotated: '2026-01-01'\n"
    )
    (root / ".git").write_text("gitdir: /nonexistent\n")
    out = _run_hook_process(f"cat {dest}", root)
    assert out and out["permissionDecision"] == "deny", out
    assert dest in out["permissionDecisionReason"]


def test_a_payload_cwd_in_a_subdirectory_of_the_hooks_checkout_still_derives_a_set():
    out = _run_hook_process(f"cat {INCIDENT}", os.path.join(_REPO, "ansible", "roles"))
    assert out and out["permissionDecision"] == "deny", out
    assert INCIDENT in out["permissionDecisionReason"]


def test_a_payload_cwd_in_no_checkout_derives_from_the_hooks_own_tree(tmp_path):
    """A cwd with no Ansible tree above it walks the hook's own tree, not an empty one."""
    out = _run_hook_process(f"cat {INCIDENT}", tmp_path)
    assert out and out["permissionDecision"] == "deny", out
    assert INCIDENT in out["permissionDecisionReason"]
