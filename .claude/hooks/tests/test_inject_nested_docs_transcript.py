#!/usr/bin/env python3
"""Tests for inject-nested-docs crediting a context with the blocks its transcript holds.

Each transcript is written as the JSONL records Claude Code writes, carrying a block the hook
itself rendered for another session, so the parser reads the hook's real output format.

Run: uv run pytest .claude/hooks/tests/test_inject_nested_docs_transcript.py
"""

import importlib.util
import json
import os
import sys
import tempfile

import pytest

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _HERE)

from _hook_common import INSTRUCTIONS_LOG_ENV  # noqa: E402
from _inline_budget import blocks  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "inject_nested_docs", os.path.join(_HERE, "inject-nested-docs.py")
)
assert _spec and _spec.loader, "spec_from_file_location found no loader"
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

ROLE = "k8s/roles/foo/CLAUDE.md"
READ = "cat k8s/roles/foo/templates/deployment.yaml.j2"
POINTER_RULE = '---\npaths:\n  - "docs/**"\n---\n\n# Generated docs\n'


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "CLAUDE.md").write_text("# root\n")
    (root / "k8s/roles/foo/templates").mkdir(parents=True)
    (root / ROLE).write_text("This role's rules.\n")
    (root / "k8s/roles/foo/templates/deployment.yaml.j2").write_text("kind: x\n")
    (root / "docs").mkdir()
    (root / "docs/page.md").write_text("# Page\n")
    (root / ".claude/rules").mkdir(parents=True)
    (root / ".claude/rules/generated-docs.md").write_text(POINTER_RULE)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setenv(INSTRUCTIONS_LOG_ENV, str(tmp_path / "instructions.log"))
    return root


def _injection(repo, command, uuid="u-inject"):
    """The attachment record of the block the hook renders for `command` in a fresh session."""
    text, _ = _mod.build_context(command, str(repo), f"other-{uuid}")
    assert text, "the fixture command injects nothing"
    attachment = {"type": "hook_additional_context", "content": [text]}
    return {"type": "attachment", "uuid": uuid, "attachment": attachment}


def _write(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return str(path)


def _docs(repo, command, transcript, session="sess-new", agent_id=None):
    _, chosen = _mod.build_context(
        command, str(repo), session, agent_id=agent_id, transcript_path=transcript
    )
    return [doc for doc, _, _ in chosen]


def test_a_resumed_session_holding_the_block_is_clean(repo, tmp_path):
    transcript = _write(tmp_path / "proj/sess-new.jsonl", [_injection(repo, READ)])
    assert _docs(repo, READ, transcript) == []


def test_a_session_whose_transcript_lacks_the_block_is_flagged(repo, tmp_path):
    transcript = _write(
        tmp_path / "proj/sess-new.jsonl", [{"type": "user", "uuid": "u1"}]
    )
    assert _docs(repo, READ, transcript) == [ROLE]


def test_a_fork_inheriting_the_block_by_reference_is_clean(repo, tmp_path):
    main = _write(
        tmp_path / "proj/sess-a.jsonl",
        [_injection(repo, READ, "u1"), {"type": "user", "uuid": "u2"}],
    )
    ref = {
        "type": "fork-context-ref",
        "parentSessionId": "sess-a",
        "parentLastUuid": "u2",
    }
    _write(tmp_path / "proj/sess-a/subagents/agent-f1.jsonl", [ref])
    assert _docs(repo, READ, main, session="sess-a", agent_id="f1") == []


def test_a_block_after_the_forks_cut_is_flagged(repo, tmp_path):
    main = _write(
        tmp_path / "proj/sess-a.jsonl",
        [{"type": "user", "uuid": "u1"}, _injection(repo, READ, "u2")],
    )
    ref = {
        "type": "fork-context-ref",
        "parentSessionId": "sess-a",
        "parentLastUuid": "u1",
    }
    _write(tmp_path / "proj/sess-a/subagents/agent-f1.jsonl", [ref])
    assert _docs(repo, READ, main, session="sess-a", agent_id="f1") == [ROLE]


def test_a_fork_holding_a_copied_block_is_clean(repo, tmp_path):
    main = str(tmp_path / "proj/sess-a.jsonl")
    _write(tmp_path / "proj/sess-a/subagents/agent-f1.jsonl", [_injection(repo, READ)])
    assert _docs(repo, READ, main, session="sess-a", agent_id="f1") == []


def test_a_block_before_a_compaction_is_flagged(repo, tmp_path):
    compact = {"type": "system", "subtype": "compact_boundary", "uuid": "u2"}
    transcript = _write(
        tmp_path / "proj/sess-new.jsonl", [_injection(repo, READ, "u1"), compact]
    )
    assert _docs(repo, READ, transcript) == [ROLE]


def test_an_inherited_pointer_still_lets_a_write_inline_the_rule(repo, tmp_path):
    rule = ".claude/rules/generated-docs.md"
    transcript = _write(
        tmp_path / "proj/sess-new.jsonl", [_injection(repo, "grep x docs/page.md")]
    )
    assert _docs(repo, "grep -n x docs/page.md", transcript) == []
    assert _docs(repo, "sed -i s/a/b/ docs/page.md", transcript) == [rule]


def test_a_header_quoted_inside_a_line_does_not_split_a_block():
    head = f"===== {ROLE} (applies to `k8s/roles/foo`) ====="
    text = f"{head}\nSee `===== other.md (applies to `x`) =====` for the shape.\n"
    assert [doc for doc, _ in blocks(text)] == [ROLE]
