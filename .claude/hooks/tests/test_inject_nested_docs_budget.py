#!/usr/bin/env python3
"""Tests for inject-nested-docs' per-session budget and its short preamble.

It reuses test_inject_nested_docs.py's fixture repo. `_spend_the_budget` fills the session
budget with one role doc, so the next doc a test names meets the spent budget.

Run: uv run pytest .claude/hooks/tests/test_inject_nested_docs_budget.py
"""

import test_inject_nested_docs as base

# Bound by assignment, not imported, so the test arguments naming it do not read as a
# redefinition of an unused import (F811).
repo = base.repo
_build, _mod = base._build, base._mod

FOO_FILE = "ansible/roles/k8s/foo/templates/deployment.yaml.j2"
BAR_DOC = "ansible/roles/k8s/bar/CLAUDE.md"
# About 2 KB, so it fits the session budget alone but not after `_spend_the_budget`.
BAR_TEXT = (
    "# Bar\n\n## Bar rules\n\nthe bar body rule\n\n## Bar deploy\n\n"
    + "more bar text on this line\n" * 70
)
# Each rule's filler outgrows what `_spend_the_budget` leaves, so only the exemption inlines it.
_RULE_FILLER = "a rule line that the budget alone would outline\n" * 40
SECRETS_RULE = (
    '---\npaths:\n  - "ansible/vars/**"\n---\n\n# Secrets body\n' + _RULE_FILLER
)
GENERATED_RULE = (
    '---\npaths:\n  - "docs/**"\n---\n\n# Generated docs body\n' + _RULE_FILLER
)


def _spend_the_budget(repo, session="sess-a", agent_id=None):
    """Inline foo's role doc, about 6.5 KB: under one payload, most of the session budget."""
    (repo / "ansible" / "roles" / "k8s" / "foo" / "CLAUDE.md").write_text(
        "foo doc line with some words to fill the session budget\n" * 115
    )
    _, chosen = _mod.build_context(
        f"cat {FOO_FILE}", str(repo), session, agent_id=agent_id
    )
    assert [doc for doc, _, block in chosen if _mod._form(block) is None] == [
        "ansible/roles/k8s/foo/CLAUDE.md"
    ]


def _add_bar(repo):
    (repo / "ansible" / "roles" / "k8s" / "bar").mkdir()
    (repo / BAR_DOC).write_text(BAR_TEXT)


def test_a_role_doc_inside_the_budget_is_inlined_whole(repo):
    _add_bar(repo)
    context, chosen = _build(repo, f"cat {BAR_DOC}")
    assert [d for d, _ in chosen] == [BAR_DOC]
    assert "the bar body rule" in context


def test_a_role_doc_after_the_budget_is_spent_is_its_outline(repo):
    _spend_the_budget(repo)
    _add_bar(repo)
    context, chosen = _build(repo, f"cat {BAR_DOC}")
    assert [d for d, _ in chosen] == [BAR_DOC]
    assert "## Bar rules" in context and "## Bar deploy" in context
    assert "the bar body rule" not in context
    assert f"Read `{BAR_DOC}`" in context


def test_an_outlined_doc_is_not_sent_again(repo):
    _spend_the_budget(repo)
    _add_bar(repo)
    _build(repo, f"cat {BAR_DOC}")
    assert _build(repo, f"cat {BAR_DOC}") == ("", [])


def test_the_secrets_rule_is_whole_on_a_grep_after_the_budget_is_spent(repo):
    (repo / ".claude" / "rules" / "secrets.md").write_text(SECRETS_RULE)
    _spend_the_budget(repo)
    context, chosen = _build(repo, "grep -c sops ansible/vars/secrets.yml")
    assert [d for d, _ in chosen] == [".claude/rules/secrets.md"]
    assert _RULE_FILLER in context


def test_the_generated_docs_rule_is_whole_on_a_read_after_the_budget_is_spent(repo):
    (repo / ".claude" / "rules" / "generated-docs.md").write_text(GENERATED_RULE)
    (repo / "docs").mkdir()
    (repo / "docs" / "page.md").write_text("# Page\n")
    _spend_the_budget(repo)
    context, chosen = _build(repo, "grep -n Page docs/page.md")
    assert [d for d, _ in chosen] == [".claude/rules/generated-docs.md"]
    assert _RULE_FILLER in context


def test_a_subagent_has_its_own_budget(repo):
    _spend_the_budget(repo)
    _add_bar(repo)
    context, _ = _mod.build_context(
        f"cat {BAR_DOC}", str(repo), "sess-a", agent_id="a1"
    )
    assert "the bar body rule" in context


def test_the_first_injection_carries_the_full_preamble(repo):
    context, _ = _build(repo, f"cat {FOO_FILE}")
    assert context.startswith(_mod._PREAMBLE)


def test_a_later_injection_carries_the_one_line_tag(repo):
    _build(repo, f"cat {FOO_FILE}")
    _add_bar(repo)
    context, _ = _build(repo, f"cat {BAR_DOC}")
    assert context.startswith(_mod._SHORT_PREAMBLE)
    assert _mod._PREAMBLE not in context


def test_an_outline_row_carries_form_outline(repo, tmp_path):
    _spend_the_budget(repo, session="sess-outline-01")
    _add_bar(repo)
    payload = {
        "tool_name": "Bash",
        "session_id": "sess-outline-01",
        "cwd": str(repo),
        "tool_input": {"command": f"cat {BAR_DOC}"},
    }
    assert _mod.context(payload)
    (row,) = (tmp_path / "instructions.log").read_text().splitlines()
    assert row.split()[-2] == "form=outline"
