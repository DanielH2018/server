"""Tests for scripts/dev/findings.py: the planners are pure, gh is never called.

Run: uv run pytest scripts/dev/tests/test_findings.py
"""

from __future__ import annotations

import json
import subprocess

import findings


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


def test_run_dry_prints_and_does_not_call_gh(monkeypatch, capsys):
    monkeypatch.setattr(
        findings,
        "gh",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("gh called")),
    )
    findings.run([["issue", "comment", "1", "--body", "x"]], dry_run=True)
    assert "gh issue comment 1 --body x" in capsys.readouterr().out


def test_run_wet_calls_gh_in_order(monkeypatch):
    calls = []
    monkeypatch.setattr(findings, "gh", lambda *a, **k: calls.append(list(a)))
    findings.run([["a"], ["b"]], dry_run=False)
    assert calls == [["a"], ["b"]]


# --- list CLI -------------------------------------------------------------------------------


def test_list_json_emits_rows(monkeypatch, capsys):
    monkeypatch.setattr(
        findings,
        "gh_json",
        lambda *a, **k: [_issue(5, labels=("severity/low",), fp="f")],
    )
    assert findings.main(["list", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["number"] == 5 and rows[0]["severity"] == "low"


def test_list_passes_the_state_filter_to_gh(monkeypatch):
    seen = {}

    def fake(*a, **k):
        seen["argv"] = a
        return []

    monkeypatch.setattr(findings, "gh_json", fake)
    findings.main(["list", "--state", "closed"])
    argv = list(seen["argv"])
    assert argv[argv.index("--state") + 1] == "closed"
    assert argv[argv.index("--label") + 1] == "claude"


# --- --dry-run parses on either side of the subcommand -----------------------------------


def test_dry_run_is_accepted_after_the_subcommand(monkeypatch):
    monkeypatch.setattr(
        findings, "gh_json", lambda *a, **k: [{"name": n} for n in findings.LABELS]
    )
    monkeypatch.setattr(
        findings,
        "gh",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("gh called")),
    )
    assert findings.main(["sync-labels", "--dry-run"]) == 0


def test_dry_run_is_accepted_before_the_subcommand(monkeypatch):
    monkeypatch.setattr(
        findings, "gh_json", lambda *a, **k: [{"name": n} for n in findings.LABELS]
    )
    monkeypatch.setattr(
        findings,
        "gh",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("gh called")),
    )
    assert findings.main(["--dry-run", "sync-labels"]) == 0


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


def _all_labels_exist(monkeypatch):
    """Every `open` CLI test needs this: cmd_open syncs labels before it reads issues."""
    monkeypatch.setattr(findings, "_existing_labels", lambda: set(findings.LABELS))


def test_open_cli_exits_3_on_refuted(monkeypatch, tmp_path):
    _all_labels_exist(monkeypatch)
    body = tmp_path / "b.md"
    body.write_text("B")
    fp = findings.fingerprint("T", "a.py:1")
    monkeypatch.setattr(
        findings,
        "load_issues",
        lambda state="all": [_issue(3, state="CLOSED", labels=("refuted",), fp=fp)],
    )
    monkeypatch.setattr(
        findings,
        "gh",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("gh called")),
    )
    assert (
        findings.main(
            [
                "open",
                "--title",
                "T",
                "--body-file",
                str(body),
                "--severity",
                "low",
                "--kind",
                "gap",
                "--file",
                "a.py:1",
            ]
        )
        == 3
    )


def test_open_cli_prints_the_created_number(monkeypatch, tmp_path, capsys):
    _all_labels_exist(monkeypatch)
    body = tmp_path / "b.md"
    body.write_text("B")
    monkeypatch.setattr(findings, "load_issues", lambda state="all": [])

    class _P:
        stdout = "https://github.com/o/r/issues/42\n"

    monkeypatch.setattr(findings, "gh", lambda *a, **k: _P())
    assert (
        findings.main(
            [
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
        )
        == 0
    )
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


def test_touch_cli_loads_the_issue_by_number(monkeypatch, capsys):
    monkeypatch.setattr(findings, "gh_json", lambda *a, **k: _issue(12))
    calls = []
    monkeypatch.setattr(findings, "gh", lambda *a, **k: calls.append(list(a)))
    assert findings.main(["touch", "12", "--source", "review-2026-09-02"]) == 0
    assert calls[0][:3] == ["issue", "comment", "12"]
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


def test_close_refuted_without_a_reason_is_rejected_before_any_write(monkeypatch):
    monkeypatch.setattr(
        findings,
        "gh",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("gh called")),
    )
    assert findings.main(["close", "5", "--refuted"]) == 2


def test_close_refuted_with_a_pr_is_rejected(monkeypatch):
    monkeypatch.setattr(
        findings,
        "gh",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("gh called")),
    )
    assert (
        findings.main(["close", "5", "--refuted", "--reason", "r", "--pr", "700"]) == 2
    )


def test_close_fixed_with_a_pr_is_accepted(monkeypatch):
    calls = []
    monkeypatch.setattr(findings, "gh", lambda *a, **k: calls.append(list(a)))
    assert findings.main(["close", "5", "--fixed", "--pr", "700"]) == 0
    assert calls[0][:3] == ["issue", "close", "5"]


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


class _CreatedProcess:
    stdout = "https://github.com/o/r/issues/42\n"


def test_open_retries_without_project_and_warns(monkeypatch, tmp_path, capsys):
    _all_labels_exist(monkeypatch)
    body = tmp_path / "b.md"
    body.write_text("B")
    monkeypatch.setattr(findings, "load_issues", lambda state="all": [])
    calls = []

    def fake_gh(*argv, **kwargs):
        calls.append(list(argv))
        if len(calls) == 1:
            raise subprocess.CalledProcessError(
                1, "gh", stderr="could not resolve to a ProjectV2\nmore\n"
            )
        return _CreatedProcess()

    monkeypatch.setattr(findings, "gh", fake_gh)
    assert findings.main(_open_argv(body)) == 0
    out = capsys.readouterr()
    assert "#42 created" in out.out
    assert 'not added to Project "Claude findings": could not resolve' in out.err
    assert "--project" in calls[0] and "--project" not in calls[1]


def test_open_propagates_a_non_project_failure(monkeypatch, tmp_path):
    _all_labels_exist(monkeypatch)
    body = tmp_path / "b.md"
    body.write_text("B")
    monkeypatch.setattr(findings, "load_issues", lambda state="all": [])

    def fake_gh(*argv, **kwargs):
        raise subprocess.CalledProcessError(1, "gh", stderr="HTTP 422: label not found")

    monkeypatch.setattr(findings, "gh", fake_gh)
    assert findings.main(_open_argv(body)) == 1


# --- open creates the labels it is about to use -----------------------------------------------


def test_open_creates_a_missing_label_before_the_issue(monkeypatch, tmp_path):
    body = tmp_path / "b.md"
    body.write_text("B")
    monkeypatch.setattr(
        findings, "_existing_labels", lambda: set(findings.LABELS) - {"kind/gap"}
    )
    monkeypatch.setattr(findings, "load_issues", lambda state="all": [])
    calls = []

    def fake_gh(*argv, **kwargs):
        calls.append(list(argv))
        return _CreatedProcess()

    monkeypatch.setattr(findings, "gh", fake_gh)
    assert findings.main(_open_argv(body)) == 0
    assert calls[0][:3] == ["label", "create", "kind/gap"]
    assert calls[1][:2] == ["issue", "create"]


def test_open_with_every_label_present_creates_none(monkeypatch, tmp_path):
    _all_labels_exist(monkeypatch)
    body = tmp_path / "b.md"
    body.write_text("B")
    monkeypatch.setattr(findings, "load_issues", lambda state="all": [])
    calls = []

    def fake_gh(*argv, **kwargs):
        calls.append(list(argv))
        return _CreatedProcess()

    monkeypatch.setattr(findings, "gh", fake_gh)
    assert findings.main(_open_argv(body)) == 0
    assert all(argv[:2] != ["label", "create"] for argv in calls)


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


def test_open_with_a_missing_body_file_exits_2_without_calling_gh(
    monkeypatch, tmp_path
):
    def boom(*a, **k):
        raise AssertionError("gh called")

    monkeypatch.setattr(findings, "gh", boom)
    monkeypatch.setattr(findings, "_existing_labels", boom)
    assert findings.main(_open_argv(tmp_path / "absent.md")) == 2


def test_a_gh_timeout_exits_1(monkeypatch, tmp_path, capsys):
    _all_labels_exist(monkeypatch)
    body = tmp_path / "b.md"
    body.write_text("B")

    def boom(state="all"):
        raise subprocess.TimeoutExpired("gh", 60)

    monkeypatch.setattr(findings, "load_issues", boom)
    assert findings.main(_open_argv(body)) == 1
    assert "gh failed:" in capsys.readouterr().err


# --- a closed issue's date and state -----------------------------------------------------------


def test_open_on_a_refuted_issue_with_a_null_closed_at(monkeypatch, tmp_path, capsys):
    _all_labels_exist(monkeypatch)
    body = tmp_path / "b.md"
    body.write_text("B")
    existing = _issue(
        3, state="CLOSED", labels=("refuted",), fp=findings.fingerprint("T", None)
    )
    existing["closedAt"] = None
    monkeypatch.setattr(findings, "load_issues", lambda state="all": [existing])
    monkeypatch.setattr(
        findings,
        "gh",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("gh called")),
    )
    assert findings.main(_open_argv(body)) == 3
    assert "refuted" in capsys.readouterr().out


def test_touch_refuses_a_closed_issue(monkeypatch, capsys):
    monkeypatch.setattr(
        findings,
        "gh_json",
        lambda *a, **k: _issue(12, state="CLOSED", labels=("refuted",)),
    )
    monkeypatch.setattr(
        findings,
        "gh",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("gh called")),
    )
    assert findings.main(["touch", "12"]) == 3
    assert "closed (refuted)" in capsys.readouterr().out


# --- label prefixes resolve deterministically ---------------------------------------------------


def test_prefixed_picks_the_alphabetically_first_of_two():
    for names in ({"domain/network", "domain/cicd"}, {"domain/cicd", "domain/network"}):
        assert findings._prefixed(names, "domain/") == "cicd"
