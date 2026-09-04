"""Tests for findings.py's model, its label/touch/close plans and the list CLI over them.

The boundaries come from the `make_tools` fixture, which answers `gh_json` by argv, so
`load_issues`, `_existing_labels` and the label planning they feed all run for real here.
The `open` planner and its CLI are in test_findings_open.py; everything verify-by is in
test_findings_verify.py.

Run: uv run pytest scripts/dev/tests/test_findings.py
"""

import json

from _findings_fakes import Fakes
from dev import findings
from dev.findings_gh import run
from dev.findings_model import (
    LABELS,
    _prefixed,
    find_by_fingerprint,
    fingerprint,
    issue_rows,
    reobservations,
    sort_key,
    verify_by_section,
)
from dev.findings_plans import plan_close, plan_sync_labels, plan_touch

# --- fingerprint -----------------------------------------------------------------------


def test_fingerprint_ignores_line_numbers():
    a = fingerprint("Probe skips a role", "scripts/diagnostics/probe.py:120")
    b = fingerprint("Probe skips a role", "scripts/diagnostics/probe.py:131-140")
    assert a == b and len(a) == 12


def test_fingerprint_ignores_case_and_punctuation_in_the_title():
    a = fingerprint("Probe skips a role!", "x.py")
    b = fingerprint("probe  skips A role", "x.py")
    assert a == b


def test_fingerprint_changes_with_the_file():
    assert fingerprint("t", "a.py") != fingerprint("t", "b.py")


def test_fingerprint_without_a_file_is_stable():
    assert fingerprint("t", None) == fingerprint("t", "")


# --- lookup ----------------------------------------------------------------------------


def test_find_by_fingerprint_matches_the_trailer_only(issue):
    hit = issue(3, fp="abc123def456")
    miss = issue(4)
    miss["body"] = "mentions abc123def456 in prose"
    found = find_by_fingerprint([miss, hit], "abc123def456")
    assert found is not None, "the fingerprinted issue was not matched at all"
    assert found["number"] == 3
    assert find_by_fingerprint([miss], "abc123def456") is None


def test_reobservations_counts_only_reobserved_comments(issue):
    one = issue(
        1,
        comments=(
            "Re-observed by review-2026-08-20",
            "unrelated",
            "Re-observed by review-2026-08-27",
        ),
    )
    assert reobservations(one) == 2


# --- rows and order ---------------------------------------------------------------------


def test_issue_rows_reads_labels_into_fields(issue):
    one = issue(
        9,
        labels=("claude", "severity/high", "kind/gap", "domain/network", "escalated"),
        fp="f",
    )
    (row,) = issue_rows([one])
    assert (
        row["severity"] == "high"
        and row["kind"] == "gap"
        and row["domain"] == "network"
    )
    assert row["escalated"] is True and row["no_vetted_remediation"] is False
    assert row["first_seen"] == "2026-08-15" and row["number"] == 9


def test_sort_key_puts_escalated_high_before_plain_high_before_medium(issue):
    rows = issue_rows(
        [
            issue(1, labels=("severity/medium",)),
            issue(2, labels=("severity/high",)),
            issue(3, labels=("severity/high", "escalated")),
            issue(4, labels=()),
        ]
    )
    assert [r["number"] for r in sorted(rows, key=sort_key)] == [3, 2, 1, 4]


def test_issue_rows_reads_verify_by_presence(issue):
    with_it = issue(1, fp="a" * 12)
    with_it["body"] += verify_by_section("true")
    without = issue(2, fp="b" * 12)
    rows = {r["number"]: r for r in issue_rows([with_it, without])}
    assert rows[1]["verify_by"] is True
    assert rows[2]["verify_by"] is False


# --- sync-labels -------------------------------------------------------------------------


def test_sync_labels_with_everything_present_plans_nothing():
    assert plan_sync_labels(set(LABELS)) == []


def test_sync_labels_plans_exactly_the_missing_label():
    existing = set(LABELS) - {"escalated"}
    plans = plan_sync_labels(existing)
    assert len(plans) == 1
    assert plans[0][:3] == ["label", "create", "escalated"]
    assert "--force" not in plans[0]


# --- run and dry-run ----------------------------------------------------------------------


def test_run_dry_prints_and_does_not_call_gh(capsys, make_tools):
    tools, calls = make_tools()
    run([["issue", "comment", "1", "--body", "x"]], True, tools)
    assert "gh issue comment 1 --body x" in capsys.readouterr().out
    assert calls.none()


def test_run_wet_calls_gh_in_order(make_tools):
    tools, calls = make_tools()
    run([["a"], ["b"]], False, tools)
    assert calls.gh == [["a"], ["b"]]


# --- list CLI -------------------------------------------------------------------------------


def test_list_json_emits_rows(capsys, issue, make_tools):
    tools, _ = make_tools(Fakes(issues=[issue(5, labels=("severity/low",), fp="f")]))
    assert findings.main(["list", "--json"], tools) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["number"] == 5 and rows[0]["severity"] == "low"


def test_list_passes_the_state_filter_to_gh(make_tools):
    tools, calls = make_tools()
    findings.main(["list", "--state", "closed"], tools)
    argv = calls.gh_json[0]
    assert argv[argv.index("--state") + 1] == "closed"
    assert argv[argv.index("--label") + 1] == "claude"


# --- --dry-run parses on either side of the subcommand -----------------------------------


def test_dry_run_is_accepted_after_the_subcommand(make_tools):
    tools, calls = make_tools()
    assert findings.main(["sync-labels", "--dry-run"], tools) == 0
    assert not calls.gh


def test_dry_run_is_accepted_before_the_subcommand(make_tools):
    tools, calls = make_tools()
    assert findings.main(["--dry-run", "sync-labels"], tools) == 0
    assert not calls.gh


# --- touch -----------------------------------------------------------------------------------


def test_touch_second_sighting_does_not_escalate(issue):
    plans = plan_touch(issue(1, comments=()), "review-2026-09-02")
    assert [p[:2] for p in plans] == [["issue", "comment"]]
    assert "sighting 2" in plans[0][-1]


def test_touch_third_sighting_escalates(issue):
    plans = plan_touch(issue(1, comments=("Re-observed by a",)), "b")
    assert [p[:2] for p in plans] == [["issue", "comment"], ["issue", "edit"]]
    assert plans[1][-1] == "escalated"


def test_touch_already_escalated_does_not_add_the_label_again(issue):
    plans = plan_touch(
        issue(
            1, labels=("escalated",), comments=("Re-observed by a", "Re-observed by b")
        ),
        "c",
    )
    assert [p[:2] for p in plans] == [["issue", "comment"]]


def test_touch_cli_loads_the_issue_by_number(capsys, issue, make_tools):
    tools, calls = make_tools(Fakes(view=issue(12)))
    assert findings.main(["touch", "12", "--source", "review-2026-09-02"], tools) == 0
    assert calls.gh[0][:3] == ["issue", "comment", "12"]
    assert "#12 touched" in capsys.readouterr().out


def test_touch_refuses_a_closed_issue(capsys, issue, make_tools):
    closed = issue(12, state="CLOSED", labels=("refuted",))
    tools, calls = make_tools(Fakes(view=closed))
    assert findings.main(["touch", "12"], tools) == 3
    assert "closed (refuted)" in capsys.readouterr().out
    assert not calls.gh


# --- close -----------------------------------------------------------------------------------


def test_close_fixed_with_pr_names_the_pr():
    plans = plan_close(5, fixed=True, pr=700, reason=None)
    (argv,) = plans
    assert (
        argv[:3] == ["issue", "close", "5"]
        and "#700" in argv[argv.index("--comment") + 1]
    )
    assert "refuted" not in argv


def test_close_refuted_adds_the_label_then_closes_with_the_reason():
    plans = plan_close(
        5, fixed=False, pr=None, reason="the timer is disabled by design"
    )
    assert plans[0] == ["issue", "edit", "5", "--add-label", "refuted"]
    assert plans[1][:3] == ["issue", "close", "5"]
    assert (
        "the timer is disabled by design" in plans[1][plans[1].index("--comment") + 1]
    )


def test_close_refuted_without_a_reason_is_rejected_before_any_write(make_tools):
    tools, calls = make_tools()
    assert findings.main(["close", "5", "--refuted"], tools) == 2
    assert calls.none()


def test_close_refuted_with_a_pr_is_rejected(make_tools):
    tools, calls = make_tools()
    argv = ["close", "5", "--refuted", "--reason", "r", "--pr", "700"]
    assert findings.main(argv, tools) == 2
    assert calls.none()


def test_close_fixed_with_a_pr_is_accepted(make_tools):
    tools, calls = make_tools()
    assert findings.main(["close", "5", "--fixed", "--pr", "700"], tools) == 0
    assert calls.gh[0][:3] == ["issue", "close", "5"]


# --- the fingerprint trailer across line endings -----------------------------------------------

_CRLF_BODY = "details\r\n\r\n---\r\nFingerprint: `abc123def456`\r\nSource: s\r\n"


def test_find_by_fingerprint_matches_a_crlf_body(issue):
    crlf = issue(7)
    crlf["body"] = _CRLF_BODY
    found = find_by_fingerprint([crlf], "abc123def456")
    assert found is not None, "a CRLF body hid the fingerprint"
    assert found["number"] == 7


def test_find_by_fingerprint_rejects_a_different_id_in_a_crlf_body(issue):
    crlf = issue(7)
    crlf["body"] = _CRLF_BODY
    assert find_by_fingerprint([crlf], "0123456789ab") is None


# --- label prefixes resolve deterministically ---------------------------------------------------


def test_prefixed_picks_the_alphabetically_first_of_two():
    for names in ({"domain/network", "domain/cicd"}, {"domain/cicd", "domain/network"}):
        assert _prefixed(names, "domain/") == "cicd"
