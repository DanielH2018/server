"""B2 free-tier STORAGE headroom — the half of the B2 budget that went unwatched when kopia
retired on 2026-08-10 and took `kopia_b2_billable_bytes` with it.

The live API call is not exercised here (it spends the transaction cap it guards); these cover the
pure summing and verdict logic, and `docs/b2-transaction-cap-monitoring-gaps.md` carries the
operator smoke-test that proves B2 accepts the query.
"""

import check


def test_sum_versions_counts_every_version_including_hidden():
    """Hidden and unfinished versions bill as stored bytes and do NOT appear in a plain object
    listing — omitting them is the specific way this number reads lower than the invoice."""
    pages = [
        {"files": [{"contentLength": 1000}, {"contentLength": 2000, "action": "hide"}]},
        {"files": [{"contentLength": 3000, "action": "start"}]},
    ]
    total, count = check.b2_sum_versions(pages)
    assert total == 6000
    assert count == 3


def test_sum_versions_tolerates_the_size_field_and_missing_lengths():
    total, count = check.b2_sum_versions([{"files": [{"size": 500}, {}]}])
    assert total == 500
    assert count == 2


def test_verdict_ok_under_threshold():
    ok, msg = check.b2_storage_verdict(1e9, 10, False, cap=10e9, max_pct=80)
    assert ok
    assert "10%" in msg


def test_verdict_down_over_threshold():
    ok, msg = check.b2_storage_verdict(9e9, 10, False, cap=10e9, max_pct=80)
    assert not ok
    assert "90%" in msg


def test_verdict_treats_a_truncated_walk_as_failure():
    """Under-reporting is the dangerous direction: a partial sum looks like headroom we do not
    have, so a truncated listing must page rather than report a smaller number confidently."""
    ok, msg = check.b2_storage_verdict(1e9, 50000, True, cap=10e9, max_pct=80)
    assert not ok
    assert "FLOOR" in msg


def test_storage_api_reads_the_v3_shape():
    auth = {
        "authorizationToken": "tok",
        "apiInfo": {"storageApi": {"apiUrl": "https://api", "bucketId": "b1"}},
    }
    assert check.b2_storage_api(auth) == ("https://api", "tok", "b1")


def test_storage_api_reads_the_older_top_level_shape():
    auth = {
        "authorizationToken": "tok",
        "apiUrl": "https://api",
        "allowed": {"bucketId": "b1"},
    }
    assert check.b2_storage_api(auth) == ("https://api", "tok", "b1")


def test_storage_disabled_without_credentials(monkeypatch):
    monkeypatch.setattr(check, "B2_PROBE_KEY_ID", "")
    ok, msg = check.b2_storage_usage()
    assert ok
    assert "disabled" in msg


def test_storage_is_gated_by_b2_reachable():
    """A transaction cap fails both this and b2_reachable; one root cause must not light two
    monitors, which is precisely what the B2 gate is for."""
    assert "b2_storage" in check.B2_DEPENDENT
    assert "b2_storage" in {name for name, _, _ in check.CHECKS}
