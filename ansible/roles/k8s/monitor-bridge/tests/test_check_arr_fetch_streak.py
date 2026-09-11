"""check_arr_queue's FETCH streak: a rolling *arr is not a queue fault.

Three of this monitor's DOWN episodes over the 30 days to 2026-09-11 were
`arr_queue check error: radarr.homelab.svc.cluster.local:7878: <urlopen error [Errno 111]`,
each co-timed with a `k8s_workloads ... radarr(1)` episode — k3s replacing the pod, reported
twice. `ARR_FETCH_CONSECUTIVE` holds `up` through that.

The pairs here have to separate the two halves of the check, because a streak over the whole
thing would pass an accept/reject pair on the fetch while delaying a poisoned release in the
queue by three cycles. In its own file rather than in test_check_service.py: that module sits
at its `monkeypatch_allowlist.txt` and `module_length_allowlist.txt` entries, and both ratchets
only ever fall.
"""

from dataclasses import replace

import checks.service


def _queue(*records):
    return {"records": list(records)}


def _unreachable(*_a, **_k):
    raise OSError(
        "radarr.homelab.svc.cluster.local:7878: [Errno 111] Connection refused"
    )


def test_arr_queue_holds_a_single_unreachable_cycle(cfg):
    # The rollout case: radarr is being replaced, so its API refuses connections for a cycle or
    # two. Three DOWN episodes in the 30 days to 2026-09-11 were exactly this, each co-timed
    # with a k8s_workloads radarr(1) episode.
    cfg = replace(cfg, RADARR_API_KEY="x")
    ok, msg = checks.service.check_arr_queue(cfg, fetch=_unreachable)
    assert ok, msg
    assert "down streak 1/3 (rollout)" in msg
    assert "Radarr unreachable" in msg


def test_the_third_straight_unreachable_cycle_pages(cfg):
    # The red proof: the streak delays a fetch failure, it does not swallow one.
    cfg = replace(cfg, RADARR_API_KEY="x")
    for _ in range(2):
        assert checks.service.check_arr_queue(cfg, fetch=_unreachable)[0]
    ok, msg = checks.service.check_arr_queue(cfg, fetch=_unreachable)
    assert not ok
    assert "Radarr unreachable" in msg
    assert "Errno 111" in msg


def test_a_queue_warning_still_pages_on_the_first_cycle(cfg):
    # The streak covers the FETCH alone. A reachable *arr with a flagged item is not a
    # transient, and delaying it is the 2026-07-01 incident this check exists for.
    cfg = replace(cfg, RADARR_API_KEY="x")
    q = _queue(
        {
            "title": "Bad.Movie.2026",
            "trackedDownloadStatus": "warning",
            "trackedDownloadState": "importPending",
        }
    )
    ok, msg = checks.service.check_arr_queue(cfg, fetch=lambda *a, **k: q)
    assert not ok
    assert "down streak" not in msg


def test_a_reachable_arr_resets_the_fetch_streak(cfg):
    cfg = replace(cfg, RADARR_API_KEY="x")
    assert checks.service.check_arr_queue(cfg, fetch=_unreachable)[0]
    assert checks.service.check_arr_queue(cfg, fetch=lambda *a, **k: _queue())[0]
    ok, msg = checks.service.check_arr_queue(cfg, fetch=_unreachable)
    assert ok, msg
    assert "down streak 1/3" in msg
