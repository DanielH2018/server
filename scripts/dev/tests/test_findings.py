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
