"""Tests for scripts/docs/reference/backlog.py: rendering only, gh is stubbed.

Run: uv run pytest scripts/docs/tests/test_gen_reference_backlog.py
"""

import re

import pytest

import build_docs
from docs.reference import backlog as g


def _row(number, severity="high", escalated=False, title="t", **kw):
    base = {
        "number": number,
        "title": title,
        "state": "OPEN",
        "severity": severity,
        "kind": "gap",
        "domain": "network",
        "escalated": escalated,
        "no_vetted_remediation": False,
        "verify_by": False,
        "claimed": None,
        "first_seen": "2026-08-15",
        "reobservations": 0,
        "url": f"https://github.com/o/r/issues/{number}",
    }
    base.update(kw)
    return base


def test_render_orders_escalated_high_before_plain_high_before_medium():
    md = g.render_markdown([_row(1, "medium"), _row(2), _row(3, escalated=True)])
    assert md.index("#3") < md.index("#2") < md.index("#1")


def test_render_escapes_a_pipe_in_the_title():
    md = g.render_markdown([_row(1, title="a | b")])
    assert "a \\| b" in md and "| a | b |" not in md


def test_render_marks_no_vetted_remediation():
    md = g.render_markdown([_row(1, no_vetted_remediation=True)])
    assert "no vetted remediation" in md


def test_render_marks_a_finding_carrying_a_verify_by():
    md = g.render_markdown([_row(1, verify_by=True)])
    row = next(line for line in md.splitlines() if line.startswith("| [#1]"))
    assert row.rstrip().endswith("| ✓ |")


def test_render_leaves_the_verify_by_cell_blank_without_one():
    md = g.render_markdown([_row(1, verify_by=False)])
    row = next(line for line in md.splitlines() if line.startswith("| [#1]"))
    assert row.rstrip().endswith("| - |")


def test_render_shows_the_claiming_worktree():
    # Anchored on the full trailing shape, not just a substring: this also pins the Claim
    # column BEFORE Verify-by, since "worktree-issue-1132" and "-" are distinguishable in
    # either order — a column swap changes which cell comes last.
    md = g.render_markdown([_row(1, claimed="worktree-issue-1132")])
    row = next(line for line in md.splitlines() if line.startswith("| [#1]"))
    assert row.rstrip().endswith("| worktree-issue-1132 | - |")


def test_render_escapes_a_pipe_in_the_claiming_worktree():
    """The claim cell goes through `md_cell` like the title does (#1280).

    A branch name may carry a `|`, which silently adds a column and renders the whole table
    wrong. Paired with `test_render_shows_the_claiming_worktree` above, which proves an
    ordinary name still reaches the cell unescaped.
    """
    md = g.render_markdown([_row(1, claimed="worktree-a | b")])
    row = next(line for line in md.splitlines() if line.startswith("| [#1]"))
    assert row.rstrip().endswith("| worktree-a \\| b | - |")


def test_render_leaves_the_claim_cell_blank_without_one():
    # verify_by=True gives the two trailing cells different values ("-" and "✓"), so a
    # column swap changes this exact ending — two matching "-" cells would not have.
    md = g.render_markdown([_row(1, claimed=None, verify_by=True)])
    row = next(line for line in md.splitlines() if line.startswith("| [#1]"))
    assert row.rstrip().endswith("| - | ✓ |")


def test_render_empty_says_so_instead_of_an_empty_table():
    md = g.render_markdown([])
    assert "No open findings" in md and "|---|" not in md


# --- the settled register ---------------------------------------------------------------


def _settled(number, ruling, domain="cicd", reason="because", **kw):
    return _row(
        number,
        state="CLOSED",
        domain=domain,
        accepted=ruling == "accepted",
        refuted=ruling == "refuted",
        reason=reason,
        **kw,
    )


def test_render_groups_settled_rows_under_their_domain_with_ruling_and_reason():
    md = g.render_markdown(
        [
            _settled(
                5, "accepted", domain="container", reason="the operator lives with it"
            ),
            _settled(6, "refuted", domain="cicd", reason="disproved at a symbol"),
        ]
    )
    settled = md[md.index("## Settled findings") :]
    assert "### cicd" in settled and "### container" in settled
    assert (
        "| [#5](https://github.com/o/r/issues/5) | accepted | t | the operator lives with it |"
        in settled
    )
    assert (
        "| [#6](https://github.com/o/r/issues/6) | refuted | t | disproved at a symbol |"
        in settled
    )
    # A closed row never leaks into the open table above the register.
    assert "#5" not in md[: md.index("## Settled findings")]


def test_render_orders_settled_domains_as_the_reviewer_list_does_and_unlabelled_last():
    md = g.render_markdown(
        [
            _settled(1, "refuted", domain=None),
            _settled(2, "refuted", domain="network"),
            _settled(3, "accepted", domain="backup-observability"),
        ]
    )
    assert (
        md.index("### backup-observability")
        < md.index("### network")
        < md.index("### unlabelled")
    )


def test_render_keeps_a_closed_row_without_a_ruling_out_of_both_tables():
    md = g.render_markdown([_row(7, state="CLOSED", accepted=False, refuted=False)])
    assert "#7" not in md and "No settled findings" in md


def test_render_shows_a_dash_for_a_settled_row_whose_close_recorded_no_reason():
    md = g.render_markdown([_settled(8, "refuted", reason=None)])
    assert "| refuted | t | - |" in md


def test_main_writes_the_page_with_a_provenance_banner(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "load_issues", lambda state="open": [])
    out = tmp_path / "backlog.md"
    assert g.main(["--out", str(out)]) == 0
    text = out.read_text()
    assert text.startswith("---\ngenerated_from: scripts/docs/reference/backlog.py")


def test_build_docs_registers_the_backlog_page():
    outs = [out for _argv, out in build_docs.GENERATORS]
    assert "docs/reference/backlog.md" in outs


def test_help_carries_the_docstring_summary(capsys):
    """#1272: the description came from `__doc__.splitlines()[1]`, which is the BLANK line
    after the summary, so `--help` printed usage and then straight to `positional arguments`
    with nothing saying what the tool is for. Asserts the summary text rather than merely
    "non-empty", so a description that goes blank again fails here.
    """
    with pytest.raises(SystemExit):
        g.main(["--help"])
    # argparse wraps the description at the terminal width and the formatter colours it, so
    # the rendered text is compared with the escapes stripped and the whitespace collapsed.
    plain = " ".join(re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out).split())
    assert "the findings Claude filed as GitHub Issues" in plain
