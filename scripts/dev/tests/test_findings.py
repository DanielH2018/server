"""Tests for scripts/dev/findings.py: the planners are pure, gh is never called.

The boundaries come from `_findings_fakes.build_tools`, which answers `gh_json` by argv, so
`load_issues`, `_existing_labels` and the label planning they feed all run for real here.

Run: uv run pytest scripts/dev/tests/test_findings.py
"""

from __future__ import annotations

import json
import subprocess

import findings
from _findings_fakes import Fakes, build_tools


def _issue(
    number,
    *,
    state="OPEN",
    labels=(),
    fp=None,
    comments=(),
    created="2026-08-15T10:00:00Z",
    title="t",
):
    body = (
        f"details\n\n---\nFingerprint: `{fp}`\nSource: review-2026-08-15\n"
        if fp
        else "details"
    )
    return {
        "number": number,
        "title": title,
        "state": state,
        "labels": [{"name": n} for n in labels],
        "body": body,
        "createdAt": created,
        "url": f"https://github.com/o/r/issues/{number}",
        "comments": [{"body": c} for c in comments],
    }


def _open_argv(body):
    return [
        "open",
        "--title",
        "T",
        "--body-file",
        str(body),
        "--severity",
        "low",
        "--kind",
        "gap",
    ]


# --- fingerprint -----------------------------------------------------------------------


def test_fingerprint_ignores_line_numbers():
    a = findings.fingerprint("Probe skips a role", "scripts/diagnostics/probe.py:120")
    b = findings.fingerprint(
        "Probe skips a role", "scripts/diagnostics/probe.py:131-140"
    )
    assert a == b and len(a) == 12


def test_fingerprint_ignores_case_and_punctuation_in_the_title():
    a = findings.fingerprint("Probe skips a role!", "x.py")
    b = findings.fingerprint("probe  skips A role", "x.py")
    assert a == b


def test_fingerprint_changes_with_the_file():
    assert findings.fingerprint("t", "a.py") != findings.fingerprint("t", "b.py")


def test_fingerprint_without_a_file_is_stable():
    assert findings.fingerprint("t", None) == findings.fingerprint("t", "")


# --- lookup ----------------------------------------------------------------------------


def test_find_by_fingerprint_matches_the_trailer_only():
    hit = _issue(3, fp="abc123def456")
    miss = _issue(4)
    miss["body"] = "mentions abc123def456 in prose"
    found = findings.find_by_fingerprint([miss, hit], "abc123def456")
    assert found is not None, "the fingerprinted issue was not matched at all"
    assert found["number"] == 3
    assert findings.find_by_fingerprint([miss], "abc123def456") is None


def test_reobservations_counts_only_reobserved_comments():
    issue = _issue(
        1,
        comments=(
            "Re-observed by review-2026-08-20",
            "unrelated",
            "Re-observed by review-2026-08-27",
        ),
    )
    assert findings.reobservations(issue) == 2


# --- rows and order ---------------------------------------------------------------------


def test_issue_rows_reads_labels_into_fields():
    issue = _issue(
        9,
        labels=("claude", "severity/high", "kind/gap", "domain/network", "escalated"),
        fp="f",
    )
    (row,) = findings.issue_rows([issue])
    assert (
        row["severity"] == "high"
        and row["kind"] == "gap"
        and row["domain"] == "network"
    )
    assert row["escalated"] is True and row["no_vetted_remediation"] is False
    assert row["first_seen"] == "2026-08-15" and row["number"] == 9


def test_sort_key_puts_escalated_high_before_plain_high_before_medium():
    rows = findings.issue_rows(
        [
            _issue(1, labels=("severity/medium",)),
            _issue(2, labels=("severity/high",)),
            _issue(3, labels=("severity/high", "escalated")),
            _issue(4, labels=()),
        ]
    )
    assert [r["number"] for r in sorted(rows, key=findings.sort_key)] == [3, 2, 1, 4]


# --- sync-labels -------------------------------------------------------------------------


def test_sync_labels_with_everything_present_plans_nothing():
    assert findings.plan_sync_labels(set(findings.LABELS)) == []


def test_sync_labels_plans_exactly_the_missing_label():
    existing = set(findings.LABELS) - {"escalated"}
    plans = findings.plan_sync_labels(existing)
    assert len(plans) == 1
    assert plans[0][:3] == ["label", "create", "escalated"]
    assert "--force" not in plans[0]


# --- run and dry-run ----------------------------------------------------------------------


def test_run_dry_prints_and_does_not_call_gh(capsys):
    tools, calls = build_tools()
    findings.run([["issue", "comment", "1", "--body", "x"]], True, tools)
    assert "gh issue comment 1 --body x" in capsys.readouterr().out
    assert calls.none()


def test_run_wet_calls_gh_in_order():
    tools, calls = build_tools()
    findings.run([["a"], ["b"]], False, tools)
    assert calls.gh == [["a"], ["b"]]


# --- list CLI -------------------------------------------------------------------------------


def test_list_json_emits_rows(capsys):
    tools, _ = build_tools(Fakes(issues=[_issue(5, labels=("severity/low",), fp="f")]))
    assert findings.main(["list", "--json"], tools) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["number"] == 5 and rows[0]["severity"] == "low"


def test_list_passes_the_state_filter_to_gh():
    tools, calls = build_tools()
    findings.main(["list", "--state", "closed"], tools)
    argv = list(calls.gh_json[0])
    assert argv[argv.index("--state") + 1] == "closed"
    assert argv[argv.index("--label") + 1] == "claude"


# --- --dry-run parses on either side of the subcommand -----------------------------------


def test_dry_run_is_accepted_after_the_subcommand():
    tools, calls = build_tools()
    assert findings.main(["sync-labels", "--dry-run"], tools) == 0
    assert not calls.gh


def test_dry_run_is_accepted_before_the_subcommand():
    tools, calls = build_tools()
    assert findings.main(["--dry-run", "sync-labels"], tools) == 0
    assert not calls.gh


# --- open ----------------------------------------------------------------------------------

_LABELS = ["claude", "severity/high", "kind/gap"]


def test_open_with_no_match_plans_a_create_with_every_label():
    outcome, code, plans = findings.plan_open(
        None, title="T", body="B", labels=_LABELS, fp="f" * 12, source="s"
    )
    assert (outcome, code) == ("created", 0)
    (argv,) = plans
    assert argv[:3] == ["issue", "create", "--title"]
    for lab in _LABELS:
        assert argv[argv.index("--label", argv.index(lab) - 1) + 1] == lab
    body = argv[argv.index("--body") + 1]
    assert (
        body.startswith("B")
        and "Fingerprint: `" + "f" * 12 + "`" in body
        and "Source: s" in body
    )
    assert argv[argv.index("--project") + 1] == findings.PROJECT_TITLE


def test_open_touch_and_reopen_never_pass_project():
    for existing in (_issue(3, fp="f" * 12), _issue(3, state="CLOSED", fp="f" * 12)):
        _, _, plans = findings.plan_open(
            existing, title="T", body="B", labels=_LABELS, fp="f" * 12, source="s"
        )
        assert all("--project" not in argv for argv in plans)


def test_open_with_an_open_match_touches_instead_of_creating():
    existing = _issue(3, fp="f" * 12)
    outcome, code, plans = findings.plan_open(
        existing, title="T", body="B", labels=_LABELS, fp="f" * 12, source="s"
    )
    assert (outcome, code) == ("touched", 0)
    assert all(argv[:2] != ["issue", "create"] for argv in plans)
    assert plans[0][:3] == ["issue", "comment", "3"]


def test_open_with_a_refuted_match_refuses_and_plans_nothing():
    existing = _issue(3, state="CLOSED", labels=("refuted",), fp="f" * 12)
    outcome, code, plans = findings.plan_open(
        existing, title="T", body="B", labels=_LABELS, fp="f" * 12, source="s"
    )
    assert (outcome, code, plans) == ("refuted", 3, [])


def test_open_with_a_fixed_match_reopens_then_comments():
    existing = _issue(3, state="CLOSED", fp="f" * 12)
    outcome, code, plans = findings.plan_open(
        existing, title="T", body="B", labels=_LABELS, fp="f" * 12, source="s"
    )
    assert (outcome, code) == ("reopened", 0)
    assert plans[0][:3] == ["issue", "reopen", "3"]
    assert plans[1][:3] == ["issue", "comment", "3"]
    assert "regression" in plans[1][plans[1].index("--body") + 1].lower()


def test_open_cli_exits_3_on_refuted(tmp_path):
    body = tmp_path / "b.md"
    body.write_text("B")
    fp = findings.fingerprint("T", "a.py:1")
    refuted = _issue(3, state="CLOSED", labels=("refuted",), fp=fp)
    tools, calls = build_tools(Fakes(issues=[refuted]))
    argv = [*_open_argv(body), "--file", "a.py:1"]
    assert findings.main(argv, tools) == 3
    assert not calls.gh


def test_open_cli_prints_the_created_number(tmp_path, capsys):
    body = tmp_path / "b.md"
    body.write_text("B")
    tools, _ = build_tools()
    assert findings.main(_open_argv(body), tools) == 0
    assert "#42 created" in capsys.readouterr().out


def test_open_adds_no_vetted_remediation_and_domain_labels():
    _, _, plans = findings.plan_open(
        None,
        title="T",
        body="B",
        labels=_LABELS + ["domain/network", "no-vetted-remediation"],
        fp="f" * 12,
        source="s",
    )
    assert "no-vetted-remediation" in plans[0] and "domain/network" in plans[0]


# --- touch -----------------------------------------------------------------------------------


def test_touch_second_sighting_does_not_escalate():
    plans = findings.plan_touch(_issue(1, comments=()), "review-2026-09-02")
    assert [p[:2] for p in plans] == [["issue", "comment"]]
    assert "sighting 2" in plans[0][-1]


def test_touch_third_sighting_escalates():
    plans = findings.plan_touch(_issue(1, comments=("Re-observed by a",)), "b")
    assert [p[:2] for p in plans] == [["issue", "comment"], ["issue", "edit"]]
    assert plans[1][-1] == "escalated"


def test_touch_already_escalated_does_not_add_the_label_again():
    plans = findings.plan_touch(
        _issue(
            1, labels=("escalated",), comments=("Re-observed by a", "Re-observed by b")
        ),
        "c",
    )
    assert [p[:2] for p in plans] == [["issue", "comment"]]


def test_touch_cli_loads_the_issue_by_number(capsys):
    tools, calls = build_tools(Fakes(view=_issue(12)))
    assert findings.main(["touch", "12", "--source", "review-2026-09-02"], tools) == 0
    assert calls.gh[0][:3] == ["issue", "comment", "12"]
    assert "#12 touched" in capsys.readouterr().out


# --- close -----------------------------------------------------------------------------------


def test_close_fixed_with_pr_names_the_pr():
    plans = findings.plan_close(5, fixed=True, pr=700, reason=None)
    (argv,) = plans
    assert (
        argv[:3] == ["issue", "close", "5"]
        and "#700" in argv[argv.index("--comment") + 1]
    )
    assert "refuted" not in argv


def test_close_refuted_adds_the_label_then_closes_with_the_reason():
    plans = findings.plan_close(
        5, fixed=False, pr=None, reason="the timer is disabled by design"
    )
    assert plans[0] == ["issue", "edit", "5", "--add-label", "refuted"]
    assert plans[1][:3] == ["issue", "close", "5"]
    assert (
        "the timer is disabled by design" in plans[1][plans[1].index("--comment") + 1]
    )


def test_close_refuted_without_a_reason_is_rejected_before_any_write():
    tools, calls = build_tools()
    assert findings.main(["close", "5", "--refuted"], tools) == 2
    assert calls.none()


def test_close_refuted_with_a_pr_is_rejected():
    tools, calls = build_tools()
    argv = ["close", "5", "--refuted", "--reason", "r", "--pr", "700"]
    assert findings.main(argv, tools) == 2
    assert calls.none()


def test_close_fixed_with_a_pr_is_accepted():
    tools, calls = build_tools()
    assert findings.main(["close", "5", "--fixed", "--pr", "700"], tools) == 0
    assert calls.gh[0][:3] == ["issue", "close", "5"]


# --- the Project board is best-effort ---------------------------------------------------------


def test_without_project_drops_the_pair_by_position():
    argv = [
        "issue",
        "create",
        "--title",
        "Claude findings",
        "--project",
        "Claude findings",
    ]
    assert findings.without_project(argv) == [
        "issue",
        "create",
        "--title",
        "Claude findings",
    ]


def test_is_project_failure_matches_project_and_scope_only():
    assert findings.is_project_failure("could not resolve to a ProjectV2")
    assert findings.is_project_failure("missing required SCOPES")
    assert not findings.is_project_failure("HTTP 422: label not found")
    assert not findings.is_project_failure(None)


def test_open_retries_without_project_and_warns(tmp_path, capsys):
    body = tmp_path / "b.md"
    body.write_text("B")
    board = subprocess.CalledProcessError(
        1, "gh", stderr="could not resolve to a ProjectV2\nmore\n"
    )
    tools, calls = build_tools(Fakes(gh_errors=[board]))
    assert findings.main(_open_argv(body), tools) == 0
    out = capsys.readouterr()
    assert "#42 created" in out.out
    assert 'not added to Project "Claude findings": could not resolve' in out.err
    assert "--project" in calls.gh[0] and "--project" not in calls.gh[1]


def test_open_propagates_a_non_project_failure(tmp_path):
    body = tmp_path / "b.md"
    body.write_text("B")
    other = subprocess.CalledProcessError(1, "gh", stderr="HTTP 422: label not found")
    tools, _ = build_tools(Fakes(gh_errors=[other]))
    assert findings.main(_open_argv(body), tools) == 1


# --- open creates the labels it is about to use -----------------------------------------------


def test_open_creates_a_missing_label_before_the_issue(tmp_path):
    body = tmp_path / "b.md"
    body.write_text("B")
    tools, calls = build_tools(Fakes(labels=set(findings.LABELS) - {"kind/gap"}))
    assert findings.main(_open_argv(body), tools) == 0
    assert calls.gh[0][:3] == ["label", "create", "kind/gap"]
    assert calls.gh[1][:2] == ["issue", "create"]


def test_open_with_every_label_present_creates_none(tmp_path):
    body = tmp_path / "b.md"
    body.write_text("B")
    tools, calls = build_tools()
    assert findings.main(_open_argv(body), tools) == 0
    assert all(argv[:2] != ["label", "create"] for argv in calls.gh)


# --- the fingerprint trailer across line endings -----------------------------------------------

_CRLF_BODY = "details\r\n\r\n---\r\nFingerprint: `abc123def456`\r\nSource: s\r\n"


def test_find_by_fingerprint_matches_a_crlf_body():
    issue = _issue(7)
    issue["body"] = _CRLF_BODY
    found = findings.find_by_fingerprint([issue], "abc123def456")
    assert found is not None, "a CRLF body hid the fingerprint"
    assert found["number"] == 7


def test_find_by_fingerprint_rejects_a_different_id_in_a_crlf_body():
    issue = _issue(7)
    issue["body"] = _CRLF_BODY
    assert findings.find_by_fingerprint([issue], "0123456789ab") is None


# --- documented exits instead of tracebacks ----------------------------------------------------


def test_open_with_a_missing_body_file_exits_2_without_calling_gh(tmp_path):
    tools, calls = build_tools()
    assert findings.main(_open_argv(tmp_path / "absent.md"), tools) == 2
    # Both boundaries: the label sync `cmd_open` runs first is a gh_json call.
    assert calls.none()


def test_a_gh_timeout_exits_1(tmp_path, capsys):
    body = tmp_path / "b.md"
    body.write_text("B")
    slow = subprocess.TimeoutExpired("gh", 60)
    tools, _ = build_tools(Fakes(json_errors={"issue list": slow}))
    assert findings.main(_open_argv(body), tools) == 1
    assert "gh failed:" in capsys.readouterr().err


# --- a closed issue's date and state -----------------------------------------------------------


def test_open_on_a_refuted_issue_with_a_null_closed_at(tmp_path, capsys):
    body = tmp_path / "b.md"
    body.write_text("B")
    existing = _issue(
        3, state="CLOSED", labels=("refuted",), fp=findings.fingerprint("T", None)
    )
    existing["closedAt"] = None
    tools, calls = build_tools(Fakes(issues=[existing]))
    assert findings.main(_open_argv(body), tools) == 3
    assert "refuted" in capsys.readouterr().out
    assert not calls.gh


def test_touch_refuses_a_closed_issue(capsys):
    closed = _issue(12, state="CLOSED", labels=("refuted",))
    tools, calls = build_tools(Fakes(view=closed))
    assert findings.main(["touch", "12"], tools) == 3
    assert "closed (refuted)" in capsys.readouterr().out
    assert not calls.gh


# --- label prefixes resolve deterministically ---------------------------------------------------


def test_prefixed_picks_the_alphabetically_first_of_two():
    for names in ({"domain/network", "domain/cicd"}, {"domain/cicd", "domain/network"}):
        assert findings._prefixed(names, "domain/") == "cicd"


# --- verify-by: the body round-trip -----------------------------------------------------------


def test_verify_by_round_trips_through_the_parser():
    body = "details\n" + findings.verify_by_section("uv run pytest scripts/dev")
    assert findings.parse_verify_by(body) == "uv run pytest scripts/dev"


def test_verify_by_survives_prose_and_a_trailer_around_it():
    body = (
        "details\n"
        + findings.verify_by_section("uv run pytest scripts/dev")
        + findings.trailer("f" * 12, "session")
    )
    assert findings.parse_verify_by(body) == "uv run pytest scripts/dev"


def test_verify_by_round_trips_through_a_crlf_body():
    body = ("details\n" + findings.verify_by_section("true")).replace("\n", "\r\n")
    assert findings.parse_verify_by(body) == "true"


def test_parse_verify_by_absent_returns_none():
    assert findings.parse_verify_by("details\n\n---\nFingerprint: `f`\n") is None
    assert findings.parse_verify_by("") is None


def test_open_with_verify_by_stores_it_in_the_created_body():
    _, _, plans = findings.plan_open(
        None,
        title="T",
        body="B",
        labels=_LABELS,
        fp="f" * 12,
        source="s",
        verify_by="uv run pytest scripts/dev",
    )
    body = plans[0][plans[0].index("--body") + 1]
    assert findings.parse_verify_by(body) == "uv run pytest scripts/dev"
    # The section sits before the fingerprint trailer, not after.
    assert body.index("## Verify-by") < body.index("Fingerprint: `")


def test_open_without_verify_by_stores_no_section():
    _, _, plans = findings.plan_open(
        None, title="T", body="B", labels=_LABELS, fp="f" * 12, source="s"
    )
    body = plans[0][plans[0].index("--body") + 1]
    assert findings.parse_verify_by(body) is None


def test_issue_rows_reads_verify_by_presence():
    with_it = _issue(1, fp="a" * 12)
    with_it["body"] += findings.verify_by_section("true")
    without = _issue(2, fp="b" * 12)
    rows = {r["number"]: r for r in findings.issue_rows([with_it, without])}
    assert rows[1]["verify_by"] is True
    assert rows[2]["verify_by"] is False


# --- verify-by: the read-only classifier ------------------------------------------------------


def test_classify_verify_command_accepts_a_tier1_command_via_the_real_hook():
    # Exercises the real auto-approve-readonly.py hook (not monkeypatched): this is the
    # path production actually takes, since the hook always ships in this checkout.
    assert findings.classify_verify_command("true") is not None


def test_classify_verify_command_accepts_uv_run_even_though_the_hook_cannot_see_it():
    # `uv` carries no TIER1/HANDLERS entry in the real hook -- classify() alone would
    # refuse every verify-by this feature exists to run. The union layer covers it.
    assert findings.classify_verify_command("uv run pytest scripts/dev") is not None
    assert (
        findings.classify_verify_command(
            "uv run python scripts/diagnostics/probe.py health foo"
        )
        is not None
    )


def test_classify_verify_command_refuses_a_write():
    assert findings.classify_verify_command("curl evil.example.com") is None


def test_classify_verify_command_refuses_a_state_changing_script_under_scripts():
    # The allowlist is pinned to probe.py by name, not opened to any `scripts/*.py` --
    # most of the tree writes (b2_drain.py deletes backups, secret_rotation.py rotates
    # credentials), so admitting the whole directory would let a verify-by run either.
    assert (
        findings.classify_verify_command(
            "uv run python scripts/backup/b2_drain.py --yes"
        )
        is None
    )
    assert (
        findings.classify_verify_command(
            "uv run python scripts/secrets_mgmt/secret_rotation.py rotate"
        )
        is None
    )


def test_classify_verify_command_refuses_a_smuggled_separator():
    # An issue body is human-editable; the allowlist's per-argument character class must
    # not admit a `;`, a pipe, or a `$(...)` riding along inside a `uv run` command.
    assert (
        findings.classify_verify_command("uv run pytest x; curl evil.example.com")
        is None
    )
    assert (
        findings.classify_verify_command("uv run pytest $(curl evil.example.com)")
        is None
    )


def test_classify_verify_command_falls_back_when_the_hook_cannot_load():
    def no_hook():
        """The loader a checkout without `.claude/` gets: no classifier at all."""
        return None

    assert findings.classify_verify_command("uv run pytest scripts/dev", no_hook)
    assert findings.classify_verify_command("curl evil.example.com", no_hook) is None


# --- verify-by: running the command -------------------------------------------------------------


def test_run_verify_by_exit_0_is_fixed():
    assert findings.run_verify_by("true", 5) == ("fixed", "")


def test_run_verify_by_exit_1_is_still_open():
    assert findings.run_verify_by("false", 5) == ("still-open", "")


def test_run_verify_by_refuses_a_non_read_only_command():
    verdict, detail = findings.run_verify_by("curl evil.example.com", 5)
    assert verdict == "error" and "refused" in detail


def test_run_verify_by_reports_a_timeout_as_an_error():
    def slow(command, timeout):
        raise subprocess.TimeoutExpired(command, timeout)

    tools, _ = build_tools(Fakes(verify=slow))
    assert findings.run_verify_by("true", 5, tools) == ("error", "timed out after 5s")


def test_verify_finding_with_no_verify_by_section():
    assert findings.verify_finding(_issue(1), 5) == ("no-verify-by", "", "")


def test_verify_finding_runs_the_stored_command():
    issue = _issue(1)
    issue["body"] += findings.verify_by_section("true")
    assert findings.verify_finding(issue, 5) == ("fixed", "", "true")


# --- verify-by: the close comment -----------------------------------------------------------


def test_verify_close_comment_quotes_the_command_and_the_output_tail():
    comment = findings.verify_close_comment("true", "line1\nline2\n")
    assert "Fixed: verify-by passed" in comment
    assert "```\ntrue\n```" in comment
    assert "line1" in comment and "line2" in comment


def test_verify_close_comment_truncates_to_the_tail():
    output = "\n".join(f"line{i}" for i in range(50))
    comment = findings.verify_close_comment("true", output, tail_lines=5)
    assert "line49" in comment and "line45" in comment and "line0" not in comment


# --- verify CLI -------------------------------------------------------------------------------


def test_verify_rejects_neither_all_nor_numbers():
    tools, calls = build_tools()
    assert findings.main(["verify"], tools) == 2
    assert calls.none()


def test_verify_rejects_both_all_and_numbers():
    tools, calls = build_tools()
    assert findings.main(["verify", "--all", "12"], tools) == 2
    assert calls.none()


def test_verify_all_prints_one_row_per_open_finding(capsys):
    fixed = _issue(1, title="Fixed one")
    fixed["body"] += findings.verify_by_section("true")
    still_open = _issue(2, title="Still broken")
    still_open["body"] += findings.verify_by_section("false")
    untracked = _issue(3, title="No probe yet")
    tools, _ = build_tools(Fakes(issues=[fixed, still_open, untracked]))
    assert findings.main(["verify", "--all"], tools) == 0
    out = capsys.readouterr().out
    assert "#1" in out and "fixed" in out
    assert "#2" in out and "still-open" in out
    assert "#3" in out and "no-verify-by" in out


def test_verify_with_numbers_loads_each_issue_by_number(capsys):
    issue = _issue(7, title="Named directly")
    issue["body"] += findings.verify_by_section("true")
    tools, _ = build_tools(Fakes(view=issue))
    assert findings.main(["verify", "7"], tools) == 0
    assert "#7" in capsys.readouterr().out


def test_verify_close_closes_only_the_fixed_ones(capsys):
    fixed = _issue(1, title="Fixed one")
    fixed["body"] += findings.verify_by_section("true")
    still_open = _issue(2, title="Still broken")
    still_open["body"] += findings.verify_by_section("false")
    tools, calls = build_tools(Fakes(issues=[fixed, still_open]))
    assert findings.main(["verify", "--all", "--close"], tools) == 0
    assert len(calls.gh) == 1
    argv = calls.gh[0]
    assert argv[:3] == ["issue", "close", "1"]
    assert "Fixed: verify-by passed" in argv[argv.index("--comment") + 1]
    out = capsys.readouterr().out
    assert "#1 closed as fixed" in out
