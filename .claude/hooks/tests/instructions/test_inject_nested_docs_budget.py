#!/usr/bin/env python3
"""Tests for inject-nested-docs' per-session budget and its short preamble.

It reuses test_inject_nested_docs.py's fixture repo. `_spend_the_budget` fills the session
budget with one role doc, so the next doc a test names meets the spent budget.

Run: uv run pytest .claude/hooks/tests/instructions/test_inject_nested_docs_budget.py
"""

import test_inject_nested_docs as base

# Bound by assignment, not imported, so the test arguments naming it do not read as a
# redefinition of an unused import (F811).
repo = base.repo
_build, _mod = base._build, base._mod

FOO_FILE = "k8s/roles/foo/templates/deployment.yaml.j2"
BAR_DOC = "k8s/roles/bar/CLAUDE.md"
# About 2 KB, so it fits the session budget alone but not after `_spend_the_budget`.
BAR_TEXT = (
    "# Bar\n\n## Bar rules\n\nthe bar body rule\n\n## Bar deploy\n\n"
    + "more bar text on this line\n" * 70
)
# Each rule's filler outgrows what `_spend_the_budget` leaves, so only the exemption inlines it.
_RULE_FILLER = "a rule line that the budget alone would outline\n" * 40
SECRETS_RULE = '---\npaths:\n  - "secrets/**"\n---\n\n# Secrets body\n' + _RULE_FILLER
GENERATED_RULE = (
    '---\npaths:\n  - "docs/**"\n---\n\n# Generated docs body\n' + _RULE_FILLER
)


def _spend_the_budget(repo, session="sess-a", agent_id=None):
    """Inline foo's role doc, about 6.5 KB: under one payload, most of the session budget."""
    (repo / "k8s" / "roles" / "foo" / "CLAUDE.md").write_text(
        "foo doc line with some words to fill the session budget\n" * 115
    )
    _, chosen = _mod.build_context(
        f"cat {FOO_FILE}", str(repo), session, agent_id=agent_id
    )
    assert [doc for doc, _, block in chosen if _mod._form(block) is None] == [
        "k8s/roles/foo/CLAUDE.md"
    ]


def _add_bar(repo):
    (repo / "k8s" / "roles" / "bar").mkdir()
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
    context, chosen = _build(repo, "grep -c sops secrets/secrets.yml")
    assert [d for d, _ in chosen] == [".claude/rules/secrets.md"]
    assert _RULE_FILLER in context


def _add_docs_page(repo):
    (repo / ".claude" / "rules" / "generated-docs.md").write_text(GENERATED_RULE)
    (repo / "docs").mkdir()
    (repo / "docs" / "page.md").write_text("# Page\n")


def _run(repo, command, session):
    payload = {
        "tool_name": "Bash",
        "session_id": session,
        "cwd": str(repo),
        "tool_input": {"command": command},
    }
    return _mod.context(base.parse_payload(payload).bash())


def test_the_generated_docs_rule_is_a_pointer_on_a_read(repo):
    _add_docs_page(repo)
    context, chosen = _build(repo, "grep -n Page docs/page.md")
    assert [d for d, _ in chosen] == [".claude/rules/generated-docs.md"]
    assert "# Generated docs body" not in context
    assert "Read `.claude/rules/generated-docs.md`" in context


def test_a_second_read_sends_no_second_pointer(repo):
    _add_docs_page(repo)
    _, first = _mod.build_context("grep -n Page docs/page.md", str(repo), "sess-a")
    assert [_mod._form(block) for _, _, block in first] == ["pointer"]
    assert _build(repo, "cat docs/page.md") == ("", [])


def test_the_generated_docs_rule_is_whole_on_a_write_after_its_pointer(repo, tmp_path):
    """The pointer's own log row must not read as the harness having loaded the rule."""
    _add_docs_page(repo)
    assert "# Generated docs body" not in _run(repo, "cat docs/page.md", "sess-ptr-01")
    _spend_the_budget(repo, session="sess-ptr-01")
    context = _run(repo, "sed -i s/Page/Home/ docs/page.md", "sess-ptr-01")
    assert _RULE_FILLER in context
    rows = (tmp_path / "instructions.log").read_text().splitlines()
    assert "form=pointer" in rows[0].split()


def test_the_facts_rule_waits_for_a_write_to_a_claude_md(repo):
    (repo / ".claude" / "rules" / "facts.md").write_text(
        '---\npaths:\n  - "**/CLAUDE.md"\n---\n\n# Citation grammar\n'
    )
    role_doc = "k8s/roles/foo/CLAUDE.md"
    _, read = _build(repo, f"wc -c {role_doc}")
    assert [d for d, _ in read] == [role_doc]
    context, written = _build(repo, f"sed -i s/a/b/ {role_doc}")
    assert [d for d, _ in written] == [".claude/rules/facts.md"]
    assert "# Citation grammar" in context


def test_the_secrets_rule_and_preamble_are_charged_to_the_session(repo):
    """Foo's 6.5 KB doc fits the budget alone, not after the 2 KB secrets rule is charged."""
    (repo / ".claude" / "rules" / "secrets.md").write_text(SECRETS_RULE)
    (repo / "k8s" / "roles" / "foo" / "CLAUDE.md").write_text(
        "foo doc line with some words to fill the session budget\n" * 115
    )
    _build(repo, "grep -c sops secrets/secrets.yml")
    context, chosen = _mod.build_context(f"cat {FOO_FILE}", str(repo), "sess-a")
    assert [_mod._form(block) for _, _, block in chosen] == ["outline"]
    assert "foo doc line" not in context


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
    assert _mod.context(base.parse_payload(payload).bash())
    (row,) = (tmp_path / "instructions.log").read_text().splitlines()
    assert row.split()[-2] == "form=outline"
