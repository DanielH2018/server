"""Tests for verify-by: what the body stores, how it parses back, and what `verify` prints.

A verify-by is prose, so nothing here runs a command and neither does the code under test.
The parser cases are named in `PARSER_CASES` and asserted present by name, so renaming one
fails saying which member went missing rather than quietly checking a smaller set.

Run: uv run pytest scripts/dev/tests/test_findings_verify.py
"""

from _findings_fakes import Fakes
from dev import findings
import pytest

from dev.findings_lib.issue_model import parse_verify_by, trailer, verify_by_section
from dev.findings_lib.verify import verification_instructions, verification_report

# The real #1345 body text, the register's only live verify-by when this changed. It is prose
# describing a deploy, which the old executable verify-by could only ever report as an error.
LIVE_PROSE = (
    "deploy traefik with a nonexistent traefik_k8s_bouncer_plugin_version and confirm the "
    "container log shows a fresh download attempt on the second and third restart"
)

# --- the body round-trip -------------------------------------------------------------------

# Each case is (body, expected). A frozenset of the names is asserted below, so a rename
# fails naming the member rather than shrinking the census silently.
PARSER_CASES = {
    "unfenced prose, written by the current open": (
        "details" + verify_by_section("Run the drill and read the summary."),
        "Run the drill and read the summary.",
    ),
    "fenced legacy command, written before 2026-09-06": (
        "details\n\n## Verify-by\n```\nuv run pytest scripts/dev\n```\n",
        "uv run pytest scripts/dev",
    ),
    "fenced legacy command with a language tag": (
        "details\n\n## Verify-by\n```bash\nnetworkctl status eno1 | grep -q routable\n```\n",
        "networkctl status eno1 | grep -q routable",
    ),
    "prose spanning several paragraphs": (
        "details" + verify_by_section("First do this.\n\nThen check that."),
        "First do this.\n\nThen check that.",
    ),
    "prose followed by the fingerprint trailer": (
        "details" + verify_by_section(LIVE_PROSE) + trailer("abc123", "session"),
        LIVE_PROSE,
    ),
    "prose followed by another heading": (
        "details" + verify_by_section("Check the log.") + "\n## Notes\nunrelated\n",
        "Check the log.",
    ),
    "a CRLF body from the API": (
        ("details" + verify_by_section("Check the log.")).replace("\n", "\r\n"),
        "Check the log.",
    ),
    "no section at all": ("details\n\n---\nFingerprint: `f`\n", None),
    "an empty body": ("", None),
    "an empty section": ("details\n\n## Verify-by\n\n---\nFingerprint: `f`\n", None),
    # The refusing half that matters most. #1308, #1313 and #1351 each carry a heading whose
    # text says they have NO instructions. A heading pattern admitting a suffix would read
    # those explanations back as instructions and report the findings as verifiable.
    "a `deliberately omitted` heading is not a verify-by": (
        "details\n\n## Verify-by, deliberately omitted\n\nAttaching a predicate here would "
        "need the guarantee this issue says nothing provides.\n",
        None,
    ),
}

REQUIRED_PARSER_CASES = frozenset(
    {
        "unfenced prose, written by the current open",
        "fenced legacy command, written before 2026-09-06",
        "fenced legacy command with a language tag",
        "prose spanning several paragraphs",
        "prose followed by the fingerprint trailer",
        "prose followed by another heading",
        "a CRLF body from the API",
        "no section at all",
        "an empty body",
        "an empty section",
        "a `deliberately omitted` heading is not a verify-by",
    }
)


def test_every_required_parser_case_is_present():
    """Non-vacuity: an `all(...)` over a census that lost its members would pass empty."""
    assert REQUIRED_PARSER_CASES <= set(PARSER_CASES)


@pytest.mark.parametrize("name", sorted(PARSER_CASES))
def test_parse_verify_by(name):
    body, expected = PARSER_CASES[name]
    assert parse_verify_by(body) == expected


def test_a_new_section_is_not_fenced():
    """Prose, not a command: a fence would say `run this`, and nothing runs a verify-by."""
    assert "```" not in verify_by_section("Check the log.")


def test_verification_instructions_reads_the_issue_body(issue):
    one = issue(1)
    assert verification_instructions(one) is None
    one["body"] += verify_by_section(LIVE_PROSE)
    assert verification_instructions(one) == LIVE_PROSE


# --- the report ------------------------------------------------------------------------------


def test_report_prints_the_instructions_for_a_finding_that_has_them(issue):
    one = issue(1, title="Traefik startupProbe has no live red-proof")
    one["body"] += verify_by_section(LIVE_PROSE)
    out = verification_report([one])
    assert "#1  Traefik startupProbe has no live red-proof" in out
    assert "How to verify:" in out
    assert "nonexistent traefik_k8s_bouncer_plugin_version" in " ".join(out.split())
    assert "1 finding with verification instructions." in out


def test_report_names_the_findings_that_have_none(issue):
    one = issue(1, title="Documented")
    one["body"] += verify_by_section("Check the log.")
    out = verification_report([one, issue(2), issue(3)])
    assert "1 finding with verification instructions." in out
    assert "(2 have none: #2, #3.)" in out


def test_report_says_so_when_nobody_wrote_any(issue):
    """The flagged half of the line above: a register with no instructions still reports."""
    out = verification_report([issue(1), issue(2)])
    assert "0 findings with verification instructions." in out
    assert "(2 have none: #1, #2.)" in out
    assert "How to verify:" not in out


def test_report_omits_the_parenthetical_when_every_finding_has_instructions(issue):
    one = issue(1)
    one["body"] += verify_by_section("Check the log.")
    out = verification_report([one])
    assert "have none" not in out and "has none" not in out


def test_report_wraps_a_long_paragraph_and_keeps_the_indent(issue):
    one = issue(1)
    one["body"] += verify_by_section(" ".join(["word"] * 60))
    body_lines = [ln for ln in verification_report([one]).splitlines() if "word" in ln]
    assert len(body_lines) > 1
    assert all(ln.startswith("    word") for ln in body_lines)


def test_report_keeps_the_break_between_two_paragraphs(issue):
    one = issue(1)
    one["body"] += verify_by_section("First do this.\n\nThen check that.")
    lines = verification_report([one]).splitlines()
    first = lines.index("    First do this.")
    assert lines[first + 1] == ""
    assert lines[first + 2] == "    Then check that."


# --- the CLI -----------------------------------------------------------------------------------


def test_verify_rejects_neither_all_nor_numbers(make_tools):
    tools, calls = make_tools()
    assert findings.main(["verify"], tools) == 2
    assert calls.none()


def test_verify_rejects_both_all_and_numbers(make_tools):
    tools, calls = make_tools()
    assert findings.main(["verify", "--all", "12"], tools) == 2
    assert calls.none()


def test_verify_all_reports_every_open_finding(capsys, issue, make_tools):
    documented = issue(1, title="Has instructions")
    documented["body"] += verify_by_section(LIVE_PROSE)
    bare = issue(2, title="No instructions yet")
    tools, calls = make_tools(Fakes(issues=[documented, bare]))
    assert findings.main(["verify", "--all"], tools) == 0
    out = capsys.readouterr().out
    assert "#1  Has instructions" in out
    assert "How to verify:" in out
    assert "(1 has none: #2.)" in out
    # Reporting is a read. Nothing is written, and in particular nothing is closed.
    assert not calls.gh


def test_verify_with_numbers_loads_each_issue_by_number(capsys, issue, make_tools):
    one = issue(7, title="Named directly")
    one["body"] += verify_by_section("Check the log.")
    tools, calls = make_tools(Fakes(view=one))
    assert findings.main(["verify", "7"], tools) == 0
    assert "#7  Named directly" in capsys.readouterr().out
    assert calls.gh_json[0][:3] == ["issue", "view", "7"]


def test_verify_takes_no_close_flag(make_tools):
    """The removal, asserted: `verify` cannot be talked into writing anything (#1313)."""
    tools, _ = make_tools()
    with pytest.raises(SystemExit):
        findings.main(["verify", "--all", "--close"], tools)
