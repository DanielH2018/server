"""B2 free-tier STORAGE headroom — the half of the B2 budget that went unwatched when kopia
retired on 2026-08-10 and took `kopia_b2_billable_bytes` with it.

The live API call is not exercised here (it spends the transaction cap it guards); these cover the
pure summing and verdict logic, and `docs/b2-transaction-cap-monitoring-gaps.md` carries the
operator smoke-test that proves B2 accepts the query.
"""

import bridge_config
import bridge_io
import checks_b2
import check


def test_sum_versions_counts_every_version_including_hidden():
    """Hidden and unfinished versions bill as stored bytes and do NOT appear in a plain object
    listing — omitting them is the specific way this number reads lower than the invoice."""
    pages = [
        {"files": [{"contentLength": 1000}, {"contentLength": 2000, "action": "hide"}]},
        {"files": [{"contentLength": 3000, "action": "start"}]},
    ]
    total, count = checks_b2.b2_sum_versions(pages)
    assert total == 6000
    assert count == 3


def test_sum_versions_tolerates_the_size_field_and_missing_lengths():
    total, count = checks_b2.b2_sum_versions([{"files": [{"size": 500}, {}]}])
    assert total == 500
    assert count == 2


def test_verdict_ok_under_threshold():
    ok, msg = checks_b2.b2_storage_verdict(1e9, 10, False, cap=10e9, max_pct=80)
    assert ok
    assert "10%" in msg


def test_verdict_down_over_threshold():
    ok, msg = checks_b2.b2_storage_verdict(9e9, 10, False, cap=10e9, max_pct=80)
    assert not ok
    assert "90%" in msg


def test_verdict_treats_a_truncated_walk_as_failure():
    """Under-reporting is the dangerous direction:

    a partial sum looks like headroom we do not have, so a truncated listing must page rather than
    report a smaller number confidently.
    """
    ok, msg = checks_b2.b2_storage_verdict(1e9, 50000, True, cap=10e9, max_pct=80)
    assert not ok
    assert "FLOOR" in msg


def test_storage_api_reads_the_v3_shape():
    auth = {
        "authorizationToken": "tok",
        "apiInfo": {"storageApi": {"apiUrl": "https://api", "bucketId": "b1"}},
    }
    assert checks_b2.b2_storage_api(auth) == ("https://api", "tok", "b1")


def test_storage_api_reads_the_older_top_level_shape():
    auth = {
        "authorizationToken": "tok",
        "apiUrl": "https://api",
        "allowed": {"bucketId": "b1"},
    }
    assert checks_b2.b2_storage_api(auth) == ("https://api", "tok", "b1")


def test_storage_disabled_without_credentials(monkeypatch):
    monkeypatch.setattr(bridge_config, "B2_PROBE_KEY_ID", "")
    ok, msg = checks_b2.b2_storage_usage()
    assert ok
    assert "disabled" in msg


def test_storage_is_gated_by_b2_reachable():
    """A transaction cap fails both this and b2_reachable; one root cause must not light two
    monitors, which is precisely what the B2 gate is for."""
    assert "b2_storage" in check.B2_DEPENDENT
    assert "b2_storage" in {name for name, _, _ in check.CHECKS}


# ── the pagination cursor ─────────────────────────────────────────────────────────────────────
# Carried as an open register row for four review runs as "the live B2 call shape is deliberately
# unverified — testing it spends the cap it guards". That reasoning covers the AUTH shape, which
# is pinned above; it never covered the cursor. b2_list_versions had no test of any kind until
# 2026-08-23, and its failure direction is the dangerous one: `truncated` is set True ONLY by
# exhausting B2_STORAGE_MAX_PAGES, so ANY early stop — a renamed cursor field, a page carrying
# files but neither nextFileName nor nextFileId — returns (pages, False) and b2_storage_verdict
# reports a partial sum as a confident total. Dormant in production today (753 versions against a
# 1000 maxFileCount, so one page), which is exactly the shape that breaks silently the first time
# the bucket crosses 1000. None of this needs a live call: _post_json is stubbed, zero B2 spend.


def _paging_stub(pages):
    """Stub for check._post_json that replays `pages` and records each request payload."""
    sent = []
    it = iter(pages)

    def fake(url, payload, headers=None):
        sent.append(payload)
        return next(it)

    return fake, sent


def test_the_cursor_is_threaded_into_the_next_request(monkeypatch):
    fake, sent = _paging_stub(
        [
            {"files": [], "nextFileName": "b.txt", "nextFileId": "4_zid"},
            {"files": []},
        ]
    )
    monkeypatch.setattr(bridge_io, "_post_json", fake)
    pages, truncated = checks_b2.b2_list_versions("https://api", "tok", "bkt")
    assert len(pages) == 2
    assert truncated is False
    assert sent[1]["startFileName"] == "b.txt", "second request must carry nextFileName"
    assert sent[1]["startFileId"] == "4_zid", "second request must carry nextFileId"


def test_a_page_with_no_cursor_ends_the_walk(monkeypatch):
    fake, sent = _paging_stub([{"files": [{"contentLength": 1}]}])
    monkeypatch.setattr(bridge_io, "_post_json", fake)
    pages, truncated = checks_b2.b2_list_versions("https://api", "tok", "bkt")
    assert len(pages) == 1
    assert truncated is False
    assert "startFileName" not in sent[0] and "startFileId" not in sent[0]


def test_a_cursor_that_never_clears_reports_truncated(monkeypatch):
    """The page cap must fail LOUD rather than silently under-counting — b2_storage_verdict keys
    on this flag to refuse a verdict it cannot stand behind."""
    fake, _ = _paging_stub(
        [{"files": [], "nextFileName": "n", "nextFileId": "i"}]
        * bridge_config.B2_STORAGE_MAX_PAGES
    )
    monkeypatch.setattr(bridge_io, "_post_json", fake)
    pages, truncated = checks_b2.b2_list_versions("https://api", "tok", "bkt")
    assert len(pages) == bridge_config.B2_STORAGE_MAX_PAGES
    assert truncated is True


def test_a_name_only_cursor_still_paginates(monkeypatch):
    """B2 can return nextFileName without nextFileId.

    Requiring both would end the walk early and under-count — the silent direction.
    """
    fake, sent = _paging_stub([{"files": [], "nextFileName": "b.txt"}, {"files": []}])
    monkeypatch.setattr(bridge_io, "_post_json", fake)
    pages, truncated = checks_b2.b2_list_versions("https://api", "tok", "bkt")
    assert len(pages) == 2
    assert truncated is False
    assert sent[1]["startFileName"] == "b.txt"
    assert "startFileId" not in sent[1]
