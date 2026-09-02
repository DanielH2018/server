"""Tests for scripts/dev/findings.py: the planners are pure, gh is never called.

Run: uv run pytest scripts/dev/tests/test_findings.py
"""

from __future__ import annotations

import json

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
    assert findings.find_by_fingerprint([miss, hit], "abc123def456")["number"] == 3
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


def test_open_cli_exits_3_on_refuted(monkeypatch, tmp_path):
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
    outcome, _, plans = findings.plan_open(
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
