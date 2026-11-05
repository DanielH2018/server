"""The `CLAUDE.md` half of the census row in `_component_contract_row.py`.

The row's own red and green subjects carry `component.yml` text alone, and a fixture has no
`CLAUDE.md` beside it, so these hold the doc parser directly.

Run: uv run pytest ansible/tests/repo/test_component_contract_row.py
"""

from _component_contract_row import doc_gaps


def test_a_doc_contract_section_with_every_field_is_clean():
    doc = (
        "## Autonomous-role contract (x)\n\n"
        "- **Scope:** a\n- **Mode:** b\n- **Abort valve:** c\n- **Evidence:** d\n"
    )
    assert doc_gaps(doc) == []


def test_a_doc_contract_section_missing_a_field_is_flagged_though_another_has_it():
    doc = (
        "## Autonomous-role contract (x)\n\n"
        "- **Scope:** a\n- **Mode:** b\n- **Abort valves:** c\n- **Evidence:** d\n\n"
        "## Autonomous-role contract (y)\n\n"
        "- **Scope:** a\n- **Mode:** b\n- **Evidence:** d\n"
    )
    assert doc_gaps(doc) == ["Abort valves"]


def test_a_doc_with_no_contract_section_reads_as_none():
    assert doc_gaps("## Something else\n\n- **Scope:** a\n") is None
