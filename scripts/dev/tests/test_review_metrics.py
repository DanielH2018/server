"""Red-proof pair for scripts/dev/review_metrics.py's rate maths.

Run: uv run pytest scripts/dev/tests/test_review_metrics.py
"""

import review_metrics as rm


def test_false_positive_rate_computes_from_complete_counts():
    row = {"high": 2, "medium": 6, "low": 0, "refuted": 2}
    assert rm.false_positive_rate(row) == 2 / 10


def test_false_positive_rate_is_null_on_a_missing_count():
    row = {"high": 2, "medium": None, "low": 0, "refuted": 2}
    assert rm.false_positive_rate(row) is None


def test_fix_refusal_rate_computes_from_complete_counts():
    row = {"fixes_proposed": 8, "fixes_refuted": 3}
    assert rm.fix_refusal_rate(row) == 3 / 8


def test_fix_refusal_rate_is_null_when_nothing_was_proposed():
    row = {"fixes_proposed": 0, "fixes_refuted": 0}
    assert rm.fix_refusal_rate(row) is None


def test_fix_refusal_rate_is_null_on_a_missing_count():
    row = {"fixes_proposed": 8, "fixes_refuted": None}
    assert rm.fix_refusal_rate(row) is None


def test_build_table_carries_date_and_ledger_through():
    rows = [
        {
            "date": "2026-09-01",
            "ledger": "review-2026-09-01-state",
            "high": 0,
            "medium": 5,
            "low": 11,
            "refuted": 2,
            "fixes_proposed": 8,
            "fixes_refuted": 3,
        }
    ]
    table = rm.build_table(rows)
    assert table[0]["date"] == "2026-09-01"
    assert table[0]["ledger"] == "review-2026-09-01-state"
    assert table[0]["false_positive_rate"] == 2 / 18
    assert table[0]["fix_refusal_rate"] == 3 / 8


def test_validate_row_accepts_a_complete_row():
    good = {
        "date": "2026-09-01",
        "high": 0,
        "medium": 5,
        "low": 11,
        "refuted": 2,
        "downgraded": None,
        "fixes_proposed": 8,
        "fixes_confirmed_safe": 5,
        "fixes_refuted": 3,
        "prs": [685, 686],
        "ledger": "review-2026-09-01-state",
    }
    assert rm.validate_row(good) == []


def test_validate_row_flags_missing_field_and_wrong_types():
    bad = {
        "date": 20260901,
        "high": "zero",
        "medium": 5,
        "low": 11,
        "refuted": 2,
        "downgraded": None,
        "fixes_proposed": 8,
        "fixes_confirmed_safe": 5,
        "fixes_refuted": 3,
        "prs": ["685"],
        "ledger": 1,
    }
    problems = rm.validate_row(bad)
    assert any("date is not a string" in p for p in problems)
    assert any("high is not an int" in p for p in problems)
    assert any("prs is not a list" in p for p in problems)
    assert any("ledger is not a string" in p for p in problems)


def _coverage_rows():
    rows = []
    for domain in sorted(rm.REVIEW_DOMAINS):
        rows.append(
            {
                "date": "2026-09-17",
                "domain": domain,
                "agent": None,
                "status": "out_of_scope",
                "reviewed": [],
                "findings": [],
                "leads": [],
            }
        )
    rows[0].update(
        status="covered",
        agent="security-review",
        reviewed=["ansible/roles/k8s/n8n"],
        findings=["k8s/n8n/x"],
    )
    rows[1].update(
        status="clean",
        agent="homelab-cicd-reviewer",
        reviewed=["ansible/roles/setup/gitops_deploy"],
    )
    rows[2].update(
        status="hole",
        agent="homelab-docs-freshness-reviewer",
        reason="agent returned nothing",
    )
    return rows


def test_validate_coverage_accepts_one_row_per_domain():
    assert rm.validate_coverage(_coverage_rows()) == []


def test_validate_coverage_flags_absent_domain_and_evidence_state_mismatches():
    rows = _coverage_rows()
    dropped = rows.pop()  # one domain absent
    rows[1]["findings"] = ["k8s/x"]  # clean row carrying a finding
    rows[2].pop("reason")  # hole without a reason
    rows[0]["reviewed"] = []  # covered row naming nothing
    problems = rm.validate_coverage(rows)
    assert f"domain absent from the ledger: {dropped['domain']}" in problems
    assert any("clean row carries findings" in p for p in problems)
    assert any("hole row has no reason" in p for p in problems)
    assert any("covered row names nothing it reviewed" in p for p in problems)


def test_every_committed_coverage_ledger_validates():
    ledgers = sorted(rm.COVERAGE_DIR.glob("*.json"))
    assert ledgers, "no coverage ledger committed under evals/review_coverage/"
    for path in ledgers:
        assert rm.validate_coverage(rm.load_coverage(path)) == [], path.name


def test_review_domains_match_findings_domain_choices():
    # The ledger's domain names are findings.py --domain's choices minus home-assistant, which
    # /ha-review owns; a rename on either side fails here by name.
    from findings_lib.issue_model import DOMAINS

    assert rm.REVIEW_DOMAINS == frozenset(DOMAINS) - {"home-assistant"}
