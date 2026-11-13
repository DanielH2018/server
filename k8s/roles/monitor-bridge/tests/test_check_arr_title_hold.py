"""Sonarr's self-clearing title hold: the ONE queue reason check_arr_queue waits on.

Sonarr applies the "Episode has a TBA title and recently aired" hold itself and releases it
once the title arrives, so a page about it names no action a human could take. The grace is 48h
because that is upstream's OWN window — `EpisodeTitleSpecification` stops applying the rule once
the episode aired more than 48 hours ago — so an item still carrying the message past it is
stuck rather than waiting.

The pairs here have to prove the hold is narrow, not just that it exists: a mixed item, a
harder state and a missing timestamp must all keep paging. In its own file rather than in
test_check_service.py, for the reason test_check_arr_fetch_streak.py gives — that module sits at
both its ratchet entries, and they only ever fall.
"""

from datetime import datetime, timezone

import checks.service


def _queue(*records):
    return {"records": list(records)}


TBA_NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
TBA_MESSAGE = "Episode has a TBA title and recently aired"


def _tba_item(added, **over):
    item = {
        "title": "Series.S01E01.1080p",
        "trackedDownloadStatus": "warning",
        "trackedDownloadState": "importPending",
        "added": added,
        "statusMessages": [{"title": "x", "messages": [TBA_MESSAGE]}],
    }
    item.update(over)
    return item


def test_queue_warnings_tba_title_hold_inside_grace_is_clean():
    # Sonarr applies this hold itself and releases it itself, so it names no operator action.
    q = _queue(_tba_item("2026-09-27T05:00:00Z"))
    assert checks.service.queue_warnings(q, "Sonarr", TBA_NOW, 48.0) == []


def test_queue_warnings_tba_title_hold_past_grace_is_flagged():
    # Past upstream's own 48h air-date window the rule no longer applies, so an item still
    # carrying the message is stuck rather than waiting.
    q = _queue(_tba_item("2026-09-24T05:00:00Z"))
    offenders = checks.service.queue_warnings(q, "Sonarr", TBA_NOW, 48.0)
    assert len(offenders) == 1
    assert TBA_MESSAGE in offenders[0][2]


def test_queue_warnings_missing_title_hold_is_held_too():
    # The TitleMissing sibling of TitleTba, same self-clearing rule in the same upstream file.
    q = _queue(
        _tba_item(
            "2026-09-27T05:00:00Z",
            statusMessages=[
                {
                    "title": "x",
                    "messages": ["Episode does not have a title and recently aired"],
                }
            ],
        )
    )
    assert checks.service.queue_warnings(q, "Sonarr", TBA_NOW, 48.0) == []


def test_queue_warnings_custom_format_rejection_still_flagged():
    # Every other queue reason needs someone to act.
    q = _queue(
        _tba_item(
            "2026-09-27T05:00:00Z",
            statusMessages=[
                {
                    "title": "x",
                    "messages": [
                        "Not a Custom Format upgrade for existing episode file(s)"
                    ],
                }
            ],
        )
    )
    offenders = checks.service.queue_warnings(q, "Sonarr", TBA_NOW, 48.0)
    assert len(offenders) == 1
    assert "Custom Format" in offenders[0][2]


def test_queue_warnings_tba_beside_another_reason_still_flagged():
    # The hold needs EVERY reason to be self-clearing, so a mixed item keeps paging.
    q = _queue(
        _tba_item(
            "2026-09-27T05:00:00Z",
            statusMessages=[
                {
                    "title": "x",
                    "messages": [
                        TBA_MESSAGE,
                        "Not a Custom Format upgrade for existing episode file(s)",
                    ],
                }
            ],
        )
    )
    offenders = checks.service.queue_warnings(q, "Sonarr", TBA_NOW, 48.0)
    assert len(offenders) == 1


def test_queue_warnings_tba_with_harder_state_still_flagged():
    q = _queue(_tba_item("2026-09-27T05:00:00Z", trackedDownloadState="importBlocked"))
    assert len(checks.service.queue_warnings(q, "Sonarr", TBA_NOW, 48.0)) == 1


def test_queue_warnings_tba_without_added_timestamp_is_flagged():
    # No clock means no grace: a missing timestamp must not buy an item silence.
    item = _tba_item("2026-09-27T05:00:00Z")
    del item["added"]
    assert (
        len(checks.service.queue_warnings(_queue(item), "Sonarr", TBA_NOW, 48.0)) == 1
    )
