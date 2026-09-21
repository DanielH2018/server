#!/usr/bin/env python3
"""Tests for the inject-nested-docs PreToolUse hook (issue #2125).

Every rule is a `..._is_flagged` / `..._is_clean` pair: a command naming a role file injects
that role's CLAUDE.md, and a second command naming the same role in the same session does
not. A hook that injects on everything and one that injects on nothing look identical from
the passing side, so each arm carries the input it must act on AND the near miss it must
leave alone.

The fixture repo is built under tmp_path with a `.git` marker, so the tests never depend on
which checkout runs them; `KNOWN_ROLE` is the non-vacuity anchor against the real tree.

Run: uv run pytest .claude/hooks/tests/test_inject_nested_docs.py
"""

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import uuid

import pytest

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _HERE)  # inject-nested-docs.py imports _hook_common


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name.replace("-", "_"), os.path.join(_HERE, f"{name}.py")
    )
    assert spec and spec.loader, "spec_from_file_location found no loader"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_mod = _load("inject-nested-docs")

ROLE_DOC = "This role's rules.\n\n## Deploy\n\nDeploy it with care.\n"
RULE = '---\npaths:\n  - "ansible/**/*.j2"\n  - "ansible/vars/**"\n---\n\n# Rule body\n'


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A checkout with one role doc, one rule file and a root CLAUDE.md, plus a private log."""
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "CLAUDE.md").write_text("# root\n")
    role = root / "ansible" / "roles" / "k8s" / "foo"
    (role / "templates").mkdir(parents=True)
    (role / "CLAUDE.md").write_text(ROLE_DOC)
    (role / "templates" / "deployment.yaml.j2").write_text("kind: Deployment\n")
    (root / "ansible" / "vars").mkdir()
    (root / "ansible" / "vars" / "secrets.yml").write_text("sops: {}\n")
    (root / "ansible" / "plain.txt").write_text("x\n")
    (root / ".claude" / "rules").mkdir(parents=True)
    (root / ".claude" / "rules" / "ansible.md").write_text(RULE)
    monkeypatch.setattr(_mod.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(_mod._logger, "LOG", str(tmp_path / "instructions.log"))
    return root


def _build(repo, command, session="sess-a"):
    return _mod.build_context(command, str(repo), session)


# ── the red-proof pair the issue asks for ────────────────────────────────────────────


def test_first_command_naming_a_role_file_is_flagged(repo):
    context, chosen = _build(
        repo, "cat ansible/roles/k8s/foo/templates/deployment.yaml.j2"
    )
    assert [d for d, _ in chosen] == [
        "ansible/roles/k8s/foo/CLAUDE.md",
        ".claude/rules/ansible.md",
    ]
    assert ROLE_DOC.strip() in context
    assert "# Rule body" in context
    assert "root" not in [d for d, _ in chosen]


def test_second_command_naming_the_same_role_is_clean(repo):
    _build(repo, "cat ansible/roles/k8s/foo/templates/deployment.yaml.j2")
    context, chosen = _build(repo, "sed -n 1,5p ansible/roles/k8s/foo/CLAUDE.md")
    assert chosen == []
    assert context == ""


def test_a_different_session_is_flagged_again(repo):
    _build(repo, "cat ansible/roles/k8s/foo/templates/deployment.yaml.j2", session="s1")
    _, chosen = _build(
        repo, "cat ansible/roles/k8s/foo/templates/deployment.yaml.j2", "s2"
    )
    assert [d for d, _ in chosen] == [
        "ansible/roles/k8s/foo/CLAUDE.md",
        ".claude/rules/ansible.md",
    ]


# ── what selects a doc ───────────────────────────────────────────────────────────────


def test_a_command_naming_no_path_is_clean(repo):
    assert _build(repo, "git status && ls") == ("", [])


def test_a_path_that_does_not_exist_is_clean(repo):
    assert _build(repo, "cat ansible/roles/k8s/nope/templates/x.j2") == ("", [])


def test_a_glob_under_the_role_resolves_to_its_directory(repo):
    _, chosen = _build(repo, "grep -l kind ansible/roles/k8s/foo/templates/*.j2")
    assert (
        "ansible/roles/k8s/foo/CLAUDE.md",
        "ansible/roles/k8s/foo/templates",
    ) in chosen


def test_the_root_claude_md_is_never_injected(repo):
    _, chosen = _build(repo, "cat ansible/plain.txt")
    assert chosen == []


def test_a_rule_glob_selects_its_rule_and_not_another_path(repo):
    _, chosen = _build(repo, "sops ansible/vars/secrets.yml")
    assert [d for d, _ in chosen] == [".claude/rules/ansible.md"]
    assert _build(repo, "cat ansible/plain.txt", session="other") == ("", [])


def test_an_absolute_path_is_flagged(repo):
    target = (
        repo / "ansible" / "roles" / "k8s" / "foo" / "templates" / "deployment.yaml.j2"
    )
    _, chosen = _build(repo, f"head -3 '{target}'")
    assert "ansible/roles/k8s/foo/CLAUDE.md" in [d for d, _ in chosen]


def test_a_path_outside_any_checkout_is_clean(repo, tmp_path):
    outside = tmp_path / "scratch"
    outside.mkdir()
    (outside / "CLAUDE.md").write_text("# stray\n")
    (outside / "f.txt").write_text("x\n")
    assert _build(repo, f"cat {outside / 'f.txt'}") == ("", [])


def test_a_doc_the_harness_already_loaded_is_clean(repo, tmp_path):
    """A `nested_traversal` row for this session means Read already loaded the doc."""
    (tmp_path / "instructions.log").write_text(
        "2026-09-21T00:00:00Z [sess-a  ] nested_traversal Project  "
        "ansible/roles/k8s/foo/CLAUDE.md trigger=ansible/roles/k8s/foo/tasks/main.yml\n"
    )
    _, chosen = _build(repo, "cat ansible/roles/k8s/foo/templates/deployment.yaml.j2")
    assert [d for d, _ in chosen] == [".claude/rules/ansible.md"]


def test_another_sessions_log_row_does_not_suppress(repo, tmp_path):
    (tmp_path / "instructions.log").write_text(
        "2026-09-21T00:00:00Z [sess-b  ] nested_traversal Project  "
        "ansible/roles/k8s/foo/CLAUDE.md\n"
    )
    _, chosen = _build(repo, "cat ansible/roles/k8s/foo/templates/deployment.yaml.j2")
    assert "ansible/roles/k8s/foo/CLAUDE.md" in [d for d, _ in chosen]


# ── the payload budget ───────────────────────────────────────────────────────────────


def test_the_budget_sits_under_both_harness_caps():
    """Local persist at 10,000 chars; remote truncation at 8,000 chars / 200 lines (2.1.267)."""
    assert _mod.INLINE_MAX_CHARS < 8000
    assert _mod.INLINE_MAX_LINES < 200


def test_a_doc_over_the_budget_is_injected_as_an_outline(repo):
    big = (
        "# Big role\n\n## Section one\n\n"
        + ("filler line\n" * 2000)
        + "## Section two\n"
    )
    (repo / "ansible" / "roles" / "k8s" / "foo" / "CLAUDE.md").write_text(big)
    context, chosen = _build(
        repo, "cat ansible/roles/k8s/foo/templates/deployment.yaml.j2"
    )
    assert "ansible/roles/k8s/foo/CLAUDE.md" in [d for d, _ in chosen]
    assert "filler line" not in context
    assert "## Section two" in context
    assert "Read `ansible/roles/k8s/foo/CLAUDE.md`" in context
    assert len(context) < _mod.INLINE_MAX_CHARS


def test_a_doc_under_the_budget_is_inlined(repo):
    context, _ = _build(repo, "cat ansible/roles/k8s/foo/templates/deployment.yaml.j2")
    assert "Deploy it with care." in context


# ── the log row and the emitted JSON ─────────────────────────────────────────────────


def test_main_emits_additional_context_and_logs_bash_path_match(
    repo, tmp_path, monkeypatch, capsys
):
    payload = {
        "tool_name": "Bash",
        "session_id": "sess-main-0001",
        "cwd": str(repo),
        "tool_input": {
            "command": "cat ansible/roles/k8s/foo/templates/deployment.yaml.j2"
        },
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert _mod.main() == 0
    out = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    assert "permissionDecision" not in out
    assert "Deploy it with care." in out["additionalContext"]
    rows = (tmp_path / "instructions.log").read_text().splitlines()
    assert any(
        "[sess-mai]" in r
        and "bash_path_match" in r
        and "ansible/roles/k8s/foo/CLAUDE.md" in r
        and "trigger=ansible/roles/k8s/foo/templates/deployment.yaml.j2" in r
        for r in rows
    ), rows


def test_main_is_silent_for_a_non_bash_tool(repo, monkeypatch, capsys):
    payload = {
        "tool_name": "Read",
        "session_id": "x",
        "cwd": str(repo),
        "tool_input": {},
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert _mod.main() == 0
    assert capsys.readouterr().out == ""


# ── frontmatter parsing ──────────────────────────────────────────────────────────────


def test_rule_globs_reads_the_block_list_form():
    assert _mod.rule_globs(RULE) == ["ansible/**/*.j2", "ansible/vars/**"]


def test_rule_globs_reads_the_flow_form():
    assert _mod.rule_globs("---\npaths: [\"a/**\", 'b/*.md']\n---\n") == [
        "a/**",
        "b/*.md",
    ]


def test_rule_globs_without_frontmatter_is_empty():
    assert _mod.rule_globs("# no frontmatter\n") == []


# ── non-vacuity against the real tree ────────────────────────────────────────────────

# A role that exists and carries a CLAUDE.md, so a rename or move goes red here rather than
# silently emptying every selection.
KNOWN_ROLE = "ansible/roles/k8s/monitor-bridge"


def test_the_real_tree_resolves_a_known_role_to_its_doc():
    root = _mod._checkout_root(os.path.join(_REPO, KNOWN_ROLE))
    assert root is not None
    docs = _mod.docs_for(root, os.path.join(KNOWN_ROLE, "files", "registry.py"))
    assert f"{KNOWN_ROLE}/CLAUDE.md" in docs
    assert ".claude/rules/ansible.md" not in docs  # a .py matches no rule glob


def test_the_real_rules_globs_match_their_own_examples():
    root = _mod._checkout_root(_REPO)
    assert ".claude/rules/secrets.md" in _mod.docs_for(root, "ansible/vars/secrets.yml")
    assert ".claude/rules/ansible.md" in _mod.docs_for(
        root, "ansible/roles/k8s/traefik/templates/deployment.yaml.j2"
    )


# ── the real entry point ─────────────────────────────────────────────────────────────

# The `uv` running this suite (`uv run` exports its path as `UV`), PATH as the fallback.
_UV = os.environ.get("UV") or shutil.which("uv")


@pytest.mark.skipif(_UV is None, reason="no `uv` on PATH to spawn")
def test_the_shim_injects_on_the_real_hook_path():
    """Run the .sh with a real payload, the way Claude Code does.

    The in-process tests above cannot see the failure this hook is most exposed to: any
    byte on stdout before the JSON — a uv reconcile line, an import-time print — makes the
    harness read the whole output as plain text and inject nothing. The shim's own
    `readlink -f "$0"` keeps it on this checkout's `.py`, so the row it appends lands in
    this checkout's gitignored `.claude/logs/instructions.log`.
    """
    session = f"e2e-{uuid.uuid4().hex}"
    payload = {
        "tool_name": "Bash",
        "session_id": session,
        "cwd": _REPO,
        "tool_input": {"command": f"sed -n 1,5p {KNOWN_ROLE}/files/registry.py"},
    }
    run = subprocess.run(
        [os.path.join(_HERE, "inject-nested-docs.sh")],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    assert "permissionDecision" not in out
    assert f"{KNOWN_ROLE}/CLAUDE.md" in out["additionalContext"]
    log = os.path.join(_HERE, "..", "logs", "instructions.log")
    with open(log, encoding="utf-8") as fh:
        rows = fh.read().splitlines()
    assert any(f"[{session[:8]}]" in r and "bash_path_match" in r for r in rows), rows[
        -3:
    ]
