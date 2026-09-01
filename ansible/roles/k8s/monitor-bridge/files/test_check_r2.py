"""Cloudflare R2 usage against the free tier.

`r2_classify_operations` splits the two billed classes and names any action it does not know,
because an unknown action counted as free is how a breach goes unseen. `r2_usage` caches a
success and reprobes after a failure, so an R2 outage cannot storm the API.
"""

import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

import check

_REPO = Path(__file__).resolve().parents[5]


def _ops(**counts):
    return [
        {"dimensions": {"actionType": a}, "sum": {"requests": n}}
        for a, n in counts.items()
    ]


def test_r2_month_start_is_utc_first_of_month():
    now = datetime(2026, 8, 15, 13, 47, 9, tzinfo=timezone.utc).timestamp()
    assert check.r2_month_start(now) == datetime(2026, 8, 1, tzinfo=timezone.utc)


def test_r2_classify_splits_the_two_billed_classes():
    a, b, unknown = check.r2_classify_operations(
        _ops(PutObject=10, UploadPart=5, GetObject=100, HeadObject=7)
    )
    assert (a, b, unknown) == (15, 107, [])


def test_r2_classify_ignores_free_operations():
    a, b, unknown = check.r2_classify_operations(
        _ops(DeleteObject=1000, AbortMultipartUpload=50)
    )
    assert (a, b, unknown) == (0, 0, [])


def test_r2_classify_counts_unknown_actions_as_class_a_and_names_them():
    # Over-counting the tighter arm is the safe direction, and the name explains the jump.
    a, b, unknown = check.r2_classify_operations(_ops(PutObject=1, SomeNewOp=42))
    assert (a, b) == (43, 0)
    assert unknown == ["SomeNewOp"]


def test_r2_verdict_up_well_inside_the_free_tier():
    ok, msg = check.r2_usage_verdict(1_200_000_000, 0, 4_100, 88_000, [])
    assert ok
    assert "storage 1.20/10 GB (12%)" in msg
    assert "Class B 88000/10000000 (1%)" in msg


def test_r2_verdict_down_on_storage_breach():
    ok, msg = check.r2_usage_verdict(8_500_000_000, 0, 0, 0, [])
    assert not ok
    assert "storage at 85%" in msg
    assert "over 80% of free tier" in msg


def test_r2_verdict_down_on_class_b_breach_names_only_that_arm():
    ok, msg = check.r2_usage_verdict(0, 0, 0, 9_000_000, [])
    assert not ok
    assert "Class B at 90%" in msg
    assert "storage at" not in msg


def test_r2_verdict_breaches_at_exactly_the_threshold():
    ok, _ = check.r2_usage_verdict(8_000_000_000, 0, 0, 0, [])
    assert not ok


def test_r2_verdict_down_on_orphaned_multipart_uploads():
    # The quiet storage fill: these bill as bytes and never appear in an object listing.
    ok, msg = check.r2_usage_verdict(0, 500, 0, 0, [])
    assert not ok
    assert "500 incomplete multipart uploads" in msg
    assert "AbortIncompleteMultipartUpload" in msg


def test_r2_verdict_reports_unknown_actions_even_when_up():
    ok, msg = check.r2_usage_verdict(0, 0, 5, 0, ["SomeNewOp"])
    assert ok
    assert "unclassified ops counted as Class A: SomeNewOp" in msg


def test_r2_verdict_treats_a_zero_limit_as_no_limit():
    # A disabled arm must not divide by zero, and must not silently read as 0% used.
    ok, msg = check.r2_usage_verdict(5_000_000_000, 0, 0, 0, [], storage_max_gb=0)
    assert ok
    assert "no limit set" in msg


def _r2_payload(storage=None, operations=None):
    account = {
        "storage": storage if storage is not None else [],
        "operations": operations or [],
    }
    return {"data": {"viewer": {"accounts": [account]}}}


def test_r2_query_parses_storage_and_operations(monkeypatch):
    payload = _r2_payload(
        storage=[{"max": {"payloadSize": 900, "metadataSize": 100, "uploadCount": 3}}],
        operations=_ops(PutObject=2, GetObject=8),
    )
    monkeypatch.setattr(check, "_post_json", lambda *a, **k: payload)
    assert check.r2_query_usage(time.time()) == (1000, 3, 2, 8, [])


def test_r2_query_treats_an_empty_bucket_as_zero_not_a_fault(monkeypatch):
    monkeypatch.setattr(check, "_post_json", lambda *a, **k: _r2_payload())
    assert check.r2_query_usage(time.time()) == (0, 0, 0, 0, [])


def test_r2_query_raises_on_graphql_errors(monkeypatch):
    # A 200 carrying `errors` is how an under-scoped token arrives. Unchecked it would parse as a
    # zero-usage bucket — green while blind.
    monkeypatch.setattr(
        check,
        "_post_json",
        lambda *a, **k: {"data": None, "errors": [{"message": "unauthorized"}]},
    )
    with pytest.raises(RuntimeError, match="unauthorized"):
        check.r2_query_usage(time.time())


def test_r2_query_raises_when_no_account_matches(monkeypatch):
    monkeypatch.setattr(
        check, "_post_json", lambda *a, **k: {"data": {"viewer": {"accounts": []}}}
    )
    with pytest.raises(RuntimeError, match="CF_ACCOUNT_ID"):
        check.r2_query_usage(time.time())


def _arm_r2(monkeypatch):
    monkeypatch.setattr(check, "CF_ACCOUNT_ID", "acct")
    monkeypatch.setattr(check, "CF_ANALYTICS_TOKEN", "tok")
    monkeypatch.setattr(check, "R2_BUCKET", "bucket")
    monkeypatch.setattr(check, "_r2_probe", {"ts": None, "ok": True, "msg": ""})


def test_r2_usage_disabled_without_credentials(monkeypatch):
    monkeypatch.setattr(check, "CF_ANALYTICS_TOKEN", "")
    ok, msg = check.r2_usage(now=1000.0)
    assert ok and "disabled" in msg


def test_r2_usage_caches_a_success(monkeypatch):
    _arm_r2(monkeypatch)
    calls = []
    monkeypatch.setattr(
        check,
        "r2_query_usage",
        lambda now: (calls.append(now), (0, 0, 0, 0, []))[1],
    )
    check.r2_usage(now=1000.0)
    ok, msg = check.r2_usage(now=1000.0 + check.R2_PROBE_INTERVAL_S - 1)
    assert ok
    assert len(calls) == 1
    assert "checked" in msg


def test_r2_usage_reprobes_after_a_failure(monkeypatch):
    # Unlike b2_reachable, a failure is NOT cached: these calls are free, so re-probing costs
    # nothing and finds recovery a cycle sooner.
    _arm_r2(monkeypatch)
    calls = []
    monkeypatch.setattr(
        check,
        "r2_query_usage",
        lambda now: (calls.append(now), (9_000_000_000, 0, 0, 0, []))[1],
    )
    assert not check.r2_usage(now=1000.0)[0]
    assert not check.r2_usage(now=1001.0)[0]
    assert len(calls) == 2
