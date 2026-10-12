#!/usr/bin/env python3
"""Tests for inject-nested-docs' head form: a doc over the payload budget.

Split from test_inject_nested_docs.py, which reached the 500-line module cap, rather than
grown in place. It reuses that module's fixture repo, so a doc here is written over the
fixture's small role doc to push it over the budget.

Run: uv run pytest .claude/hooks/tests/test_inject_nested_docs_head.py
"""

import re

import test_inject_nested_docs as base

# Bound by assignment, not imported, so the test arguments naming it do not read as a
# redefinition of an unused import (F811).
repo = base.repo
RULE, _build, _mod = base.RULE, base._build, base._mod


def _big_doc():
    """A role doc over the budget: an opening section, then two sections past the cut."""
    return (
        "# Big role\n\n## At a glance\n\nthe opening rule\n\n"
        + (("filler " * 12).rstrip() + "\n") * 2000
        + "## Section two\n\nlate rule\n\n## Section three\n"
    )


def test_a_doc_under_the_cap_but_over_what_the_preamble_leaves_is_a_head(repo):
    """A doc in the gap between the payload left and INLINE_MAX_CHARS must not be deferred forever."""
    line = ("gap filler words here " * 5).rstrip() + "\n"
    text = "# Gap doc\n\nthe opening rule\n\n## More\n\n" + line * 67
    # Few lines and many chars, so only the char budget decides between whole and head.
    assert text.count("\n") < _mod.INLINE_MAX_LINES // 2
    assert (
        _mod.INLINE_MAX_CHARS - len(_mod._PREAMBLE) < len(text) < _mod.INLINE_MAX_CHARS
    )
    (repo / "ansible" / "roles" / "k8s" / "foo" / "CLAUDE.md").write_text(text)
    context, chosen = _build(
        repo, "cat ansible/roles/k8s/foo/templates/deployment.yaml.j2"
    )
    assert "ansible/roles/k8s/foo/CLAUDE.md" in [d for d, _ in chosen]
    assert "the opening rule" in context


def test_a_doc_over_the_budget_is_injected_as_its_head(repo):
    """Headings alone are rarely enough, so an over-budget doc ships its head text."""
    (repo / "ansible" / "roles" / "k8s" / "foo" / "CLAUDE.md").write_text(_big_doc())
    context, chosen = _build(
        repo, "cat ansible/roles/k8s/foo/templates/deployment.yaml.j2"
    )
    assert "ansible/roles/k8s/foo/CLAUDE.md" in [d for d, _ in chosen]
    assert "## At a glance" in context and "the opening rule" in context
    assert "filler filler" in context
    assert "Read `ansible/roles/k8s/foo/CLAUDE.md`" in context
    assert len(context) < _mod.INLINE_MAX_CHARS
    assert context.count("\n") < _mod.INLINE_MAX_LINES


def test_the_head_names_the_sections_it_cut_off(repo):
    """The near miss for the head: a section past the cut is named, not silently dropped."""
    (repo / "ansible" / "roles" / "k8s" / "foo" / "CLAUDE.md").write_text(_big_doc())
    context, _ = _build(repo, "cat ansible/roles/k8s/foo/templates/deployment.yaml.j2")
    assert "NOT shown above" in context
    assert "## Section two" in context and "## Section three" in context
    assert "late rule" not in context


def test_an_over_budget_doc_waits_when_little_budget_is_left(repo):
    """A head under `MIN_HEAD_CHARS` teaches less than it costs, so the doc is deferred."""
    (repo / "ansible" / "roles" / "k8s" / "foo" / "CLAUDE.md").write_text(_big_doc())
    assert (
        _mod.render(
            str(repo),
            "ansible/roles/k8s/foo/CLAUDE.md",
            "ansible/roles/k8s/foo",
            _mod.MIN_HEAD_CHARS,
            _mod.INLINE_MAX_LINES,
        )
        is None
    )


def test_a_rule_beside_an_over_budget_role_doc_is_flagged(repo):
    """A head that spends the payload first would defer a short rule to a later command that
    may never run."""
    (repo / "ansible" / "roles" / "k8s" / "foo" / "CLAUDE.md").write_text(_big_doc())
    context, chosen = _build(
        repo, "sed -i s/a/b/ ansible/roles/k8s/foo/templates/deployment.yaml.j2"
    )
    assert sorted(d for d, _ in chosen) == [
        ".claude/rules/ansible.md",
        "ansible/roles/k8s/foo/CLAUDE.md",
    ]
    assert "# Rule body" in context and "the opening rule" in context
    assert len(context) < _mod.INLINE_MAX_CHARS
    assert context.count("\n") < _mod.INLINE_MAX_LINES


def test_a_rule_too_big_to_leave_the_head_its_floor_is_deferred(repo):
    """The near miss: a rule that would push the head under its floor waits, the head lands."""
    (repo / "ansible" / "roles" / "k8s" / "foo" / "CLAUDE.md").write_text(_big_doc())
    (repo / ".claude" / "rules" / "ansible.md").write_text(
        RULE + "rule line of text here, padded out to a longer width\n" * 110
    )
    assert _mod.head_floor(str(repo), ".claude/rules/ansible.md", "x") is None
    context, chosen = _build(
        repo, "sed -i s/a/b/ ansible/roles/k8s/foo/templates/deployment.yaml.j2"
    )
    assert [d for d, _ in chosen] == ["ansible/roles/k8s/foo/CLAUDE.md"]
    assert "the opening rule" in context


def test_the_head_floor_is_the_smallest_budget_render_accepts(repo):
    """`head_floor` and `_head_block` share their arithmetic; this fails if they drift."""
    (repo / "ansible" / "roles" / "k8s" / "foo" / "CLAUDE.md").write_text(_big_doc())
    args = (str(repo), "ansible/roles/k8s/foo/CLAUDE.md", "ansible/roles/k8s/foo")
    chars, lines = _mod.head_floor(*args)
    assert _mod.render(*args, chars, lines) is not None
    assert _mod.render(*args, chars - 1, lines) is None
    assert _mod.render(*args, chars, lines - 1) is None


def test_a_doc_under_the_budget_has_no_head_floor(repo):
    args = (str(repo), "ansible/roles/k8s/foo/CLAUDE.md", "ansible/roles/k8s/foo")
    assert _mod.head_floor(*args) is None


def test_the_head_cuts_at_a_heading_rather_than_mid_section(repo):
    """A cut mid-section hands the model half a rule; the roll-back keeps whole sections."""
    lines = ["# Big role", ""] + [
        f"## S{i}\n\nrule {i} body text\n" for i in range(400)
    ]
    (repo / "ansible" / "roles" / "k8s" / "foo" / "CLAUDE.md").write_text(
        "\n".join(lines)
    )
    context, _ = _build(repo, "cat ansible/roles/k8s/foo/templates/deployment.yaml.j2")
    head = context.split("----- sections")[0]
    shown = [i for i in range(400) if f"rule {i} body" in head]
    assert shown, head[-400:]
    assert f"## S{shown[-1] + 1}" in context  # the next section is named, not shown
    assert f"rule {shown[-1] + 1} body" not in head


def test_a_head_row_logs_the_injected_size_not_the_file_size(repo, tmp_path):
    """`fact_status.py report` totals `bytes=`; a head row must count the head it delivered."""
    doc = repo / "ansible" / "roles" / "k8s" / "foo" / "CLAUDE.md"
    doc.write_text(_big_doc())
    payload = {
        "tool_name": "Bash",
        "session_id": "sess-size-0001",
        "cwd": str(repo),
        "tool_input": {
            "command": "cat ansible/roles/k8s/foo/templates/deployment.yaml.j2"
        },
    }
    block = _mod.context(payload).removeprefix(_mod._PREAMBLE)
    (row,) = (tmp_path / "instructions.log").read_text().splitlines()
    assert row.endswith(f" bytes={len(block.encode('utf-8'))}"), row
    assert len(block) < doc.stat().st_size


FOO_DOC = "ansible/roles/k8s/foo/CLAUDE.md"
FOO_FILE = "ansible/roles/k8s/foo/templates/deployment.yaml.j2"


def _head_notice(repo, text):
    """The notice line a session reads for `text` as foo's doc, and the header it sits under."""
    (repo / FOO_DOC).write_text(text)
    context, chosen = _build(repo, f"cat {FOO_FILE}")
    ((doc, trigger),) = chosen
    (notice,) = [ln for ln in context.splitlines() if ln.startswith("This file is")]
    return notice, _mod._header(doc, trigger)


def _stated(notice, unit):
    found = re.search(rf"over the ([\d,]+)-{unit}", notice)
    assert found, notice
    return int(found.group(1).replace(",", ""))


def test_notice_for_a_doc_just_over_its_budget_states_that_budget(repo):
    """Under INLINE_MAX_CHARS but over what the preamble and header leave: the 7,500 is false."""
    header = _mod._header(FOO_DOC, FOO_FILE)
    over = _mod.INLINE_MAX_CHARS - len(_mod._PREAMBLE) - len(header) - 2 + 50
    text = "# Foo\n\n" + "x" * (over - len("# Foo\n\n"))
    notice, header = _head_notice(repo, text)
    budget = _stated(notice, "char")
    assert len(text) == budget + 50 and len(text) < _mod.INLINE_MAX_CHARS
    assert _mod._fits_inline(text[:budget], header)
    assert not _mod._fits_inline(text[: budget + 1], header)
    assert "7,500" not in notice and "-line" not in notice


def test_notice_for_a_line_limited_doc_states_the_line_budget(repo):
    """Cut by the line limit while well under the char limit, so the line budget is the one named."""
    lines = [f"- line {i}" for i in range(200)]
    notice, header = _head_notice(repo, "\n".join(lines) + "\n")
    budget = _stated(notice, "line")
    assert "200 lines" in notice and "-char" not in notice
    assert _mod._fits_inline("\n".join(lines[:budget]), header)
    assert not _mod._fits_inline("\n".join(lines[: budget + 1]), header)
