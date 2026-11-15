"""The members a down service row names in its `details`, which its alert summary reads.

Split from test_check_service.py, which sits at its module-length cap.
"""

from dataclasses import replace

import checks.service
from _fake_sources import FakeSources
from bridge.types import CheckResult


def test_n8n_names_each_failing_workflow():
    single = CheckResult.of(checks.service.n8n_verdict({"A Flow": 3}, 3, 2, 2))
    systemic = CheckResult.of(
        checks.service.n8n_verdict({"A Flow": 2, "B Flow": 2}, 3, 2, 2)
    )
    assert single.details == ("A Flow",)
    assert systemic.details == ("A Flow", "B Flow")


def test_n8n_names_the_same_workflow_whatever_its_streak():
    """A detail carrying the count would start a new series every cycle the streak grows."""
    first = CheckResult.of(checks.service.n8n_verdict({"A Flow": 3}, 3, 2, 2))
    later = CheckResult.of(checks.service.n8n_verdict({"A Flow": 7}, 3, 2, 2))
    assert first.msg != later.msg
    assert first.details == later.details == ("A Flow",)


def test_arr_queue_names_the_app_and_title(cfg):
    queue = {
        "records": [
            {
                "title": "Poisoned.Episode.S01E01.exe",
                "trackedDownloadStatus": "warning",
                "trackedDownloadState": "importPending",
                "statusMessages": [{"title": "x", "messages": ["executable"]}],
            }
        ]
    }
    src = FakeSources(get_json=lambda *a, **k: queue)
    result = CheckResult.of(
        checks.service.check_arr_queue(replace(cfg, SONARR_API_KEY="x"), src)
    )
    assert result.details == ("Sonarr: Poisoned.Episode.S01E01.exe",)


def test_prowlarr_names_the_indexer_without_its_minutes(seq, cfg):
    cfg = replace(cfg, PROWLARR_API_KEY="k", PROWLARR_INDEXER_MIN_DOWN_MIN=30.0)
    status = [{"indexerId": 1, "initialFailure": "2000-01-01T00:00:00Z"}]
    src = FakeSources(get_json=seq(status, [{"id": 1, "name": "EZTV"}]))
    result = CheckResult.of(checks.service.check_prowlarr_indexers(cfg, src))
    assert "EZTV down" in result.msg
    assert result.details == ("EZTV",)
